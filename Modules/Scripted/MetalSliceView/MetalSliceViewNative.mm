#import <AppKit/AppKit.h>
#import <Metal/Metal.h>
#import <QuartzCore/QuartzCore.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>

namespace
{
constexpr MTLPixelFormat OutputFormat = MTLPixelFormatBGR10A2Unorm;

struct PixelReport
{
  uint32_t Format;
  uint32_t Width;
  uint32_t Height;
  uint32_t GrayLevels;
  uint32_t NonGrayPixels;
  uint32_t MinCode;
  uint32_t MaxCode;
  uint32_t MaxSourceCodeError;
};

void SetError(char* dst, size_t size, NSString* message)
{
  if (dst && size)
    snprintf(dst, size, "%s", message.UTF8String ?: "Unknown Metal error");
}

NSString* ShaderSource()
{
  return @R"metal(
    #include <metal_stdlib>
    using namespace metal;
    struct Vertex {
      float4 position [[position]];
      float2 uv;
    };
    vertex Vertex sliceVertex(uint id [[vertex_id]]) {
      const float2 xy[] = {{-1,-1}, {1,-1}, {-1,1}, {1,1}};
      const float2 uv[] = {{0,0}, {1,0}, {0,1}, {1,1}};
      return {float4(xy[id], 0, 1), uv[id]};
    }
    fragment float4 sliceFragment(Vertex in [[stage_in]],
      texture2d<float> ct [[texture(0)]], constant float2 &window [[buffer(0)]]) {
      constexpr sampler nearest(coord::normalized, address::clamp_to_edge, filter::nearest);
      float raw = ct.sample(nearest, in.uv).r * 65535.0;
      float gray = clamp((raw - window.x) / window.y, 0.0, 1.0);
      return float4(gray, gray, gray, 1.0);
    }
  )metal";
}
}

@interface SlicerMetal10BitSurface : NSView
@property(nonatomic, strong) id<MTLDevice> device;
@property(nonatomic, strong) id<MTLCommandQueue> queue;
@property(nonatomic, strong) id<MTLRenderPipelineState> pipeline;
@property(nonatomic, strong) CAMetalLayer* metalLayer;
- (instancetype)initWithParent:(NSView*)parent error:(NSString**)message;
- (BOOL)render:(const uint16_t*)pixels width:(NSUInteger)width height:(NSUInteger)height
    low:(float)low range:(float)range rampTest:(BOOL)rampTest
    report:(PixelReport*)report error:(NSString**)message;
@end

@implementation SlicerMetal10BitSurface

- (instancetype)initWithParent:(NSView*)parent error:(NSString**)message
{
  self = [super initWithFrame:parent.bounds];
  if (!self)
    return nil;

  self.device = MTLCreateSystemDefaultDevice();
  if (!self.device)
  {
    *message = @"No Metal device is available";
    return nil;
  }
  self.queue = [self.device newCommandQueue];
  NSError* error = nil;
  id<MTLLibrary> library = [self.device newLibraryWithSource:ShaderSource() options:nil error:&error];
  if (!library)
  {
    *message = error.localizedDescription ?: @"Could not compile the Metal slice shader";
    return nil;
  }
  auto* desc = [MTLRenderPipelineDescriptor new];
  desc.vertexFunction = [library newFunctionWithName:@"sliceVertex"];
  desc.fragmentFunction = [library newFunctionWithName:@"sliceFragment"];
  desc.colorAttachments[0].pixelFormat = OutputFormat;
  self.pipeline = [self.device newRenderPipelineStateWithDescriptor:desc error:&error];
  if (!self.pipeline || !self.queue)
  {
    *message = error.localizedDescription ?: @"Could not initialize the Metal pipeline";
    return nil;
  }

  self.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
  self.metalLayer = [CAMetalLayer layer];
  self.metalLayer.device = self.device;
  self.metalLayer.pixelFormat = OutputFormat;
  self.metalLayer.framebufferOnly = NO;
  self.metalLayer.opaque = YES;
  CGColorSpaceRef space = CGColorSpaceCreateWithName(kCGColorSpaceSRGB);
  self.metalLayer.colorspace = space;
  CGColorSpaceRelease(space);
  self.layer = self.metalLayer;
  self.wantsLayer = YES;
  [parent addSubview:self positioned:NSWindowAbove relativeTo:nil];
  return self;
}

- (NSView*)hitTest:(NSPoint)point
{
  // Let the Qt/VTK slice view continue to handle scroll, pan and zoom.
  return nil;
}

- (BOOL)render:(const uint16_t*)pixels width:(NSUInteger)width height:(NSUInteger)height
    low:(float)low range:(float)range rampTest:(BOOL)rampTest
    report:(PixelReport*)report error:(NSString**)message
{
  if (!pixels || width == 0 || height == 0 || !std::isfinite(low) || !std::isfinite(range) || range <= 0)
  {
    *message = @"Invalid CT slice or window";
    return NO;
  }
  auto* inputDesc = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatR16Unorm
    width:width height:height mipmapped:NO];
  inputDesc.storageMode = MTLStorageModeShared;
  inputDesc.usage = MTLTextureUsageShaderRead;
  id<MTLTexture> input = [self.device newTextureWithDescriptor:inputDesc];
  if (!input)
  {
    *message = @"Could not allocate the 16-bit CT texture";
    return NO;
  }
  [input replaceRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0 withBytes:pixels bytesPerRow:width * sizeof(uint16_t)];

  id<CAMetalDrawable> drawable = nil;
  id<MTLTexture> target = nil;
  if (rampTest)
  {
    auto* desc = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:OutputFormat width:width height:height mipmapped:NO];
    desc.usage = MTLTextureUsageRenderTarget;
    desc.storageMode = MTLStorageModePrivate;
    target = [self.device newTextureWithDescriptor:desc];
  }
  else
  {
    CGFloat scale = self.window.backingScaleFactor;
    if (scale <= 0)
      scale = 1;
    self.metalLayer.contentsScale = scale;
    CGSize size = CGSizeMake(round(self.bounds.size.width * scale), round(self.bounds.size.height * scale));
    if (size.width <= 0 || size.height <= 0)
    {
      *message = @"The selected Slicer view is hidden";
      return NO;
    }
    if (!CGSizeEqualToSize(self.metalLayer.drawableSize, size))
      self.metalLayer.drawableSize = size;
    drawable = [self.metalLayer nextDrawable];
    target = drawable.texture;
  }
  if (!target || target.pixelFormat != OutputFormat)
  {
    *message = @"Core Animation did not provide a BGR10A2 drawable";
    return NO;
  }
  auto* pass = [MTLRenderPassDescriptor renderPassDescriptor];
  pass.colorAttachments[0].texture = target;
  pass.colorAttachments[0].loadAction = MTLLoadActionDontCare;
  pass.colorAttachments[0].storeAction = MTLStoreActionStore;
  id<MTLCommandBuffer> command = [self.queue commandBuffer];
  id<MTLRenderCommandEncoder> encoder = [command renderCommandEncoderWithDescriptor:pass];
  if (!encoder)
  {
    *message = @"Could not encode the 10-bit Metal render pass";
    return NO;
  }
  float window[2] = {low, range};
  [encoder setRenderPipelineState:self.pipeline];
  [encoder setFragmentTexture:input atIndex:0];
  [encoder setFragmentBytes:window length:sizeof(window) atIndex:0];
  [encoder drawPrimitives:MTLPrimitiveTypeTriangleStrip vertexStart:0 vertexCount:4];
  [encoder endEncoding];

  id<MTLBuffer> readback = nil;
  NSUInteger rowBytes = (target.width * sizeof(uint32_t) + 255) & ~NSUInteger(255);
  if (report)
  {
    readback = [self.device newBufferWithLength:rowBytes * target.height options:MTLResourceStorageModeShared];
    id<MTLBlitCommandEncoder> blit = readback ? [command blitCommandEncoder] : nil;
    if (!blit)
    {
      *message = @"Could not allocate a Metal readback";
      return NO;
    }
    [blit copyFromTexture:target sourceSlice:0 sourceLevel:0 sourceOrigin:MTLOriginMake(0, 0, 0)
      sourceSize:MTLSizeMake(target.width, target.height, 1) toBuffer:readback destinationOffset:0
      destinationBytesPerRow:rowBytes destinationBytesPerImage:rowBytes * target.height];
    [blit endEncoding];
  }
  if (drawable)
    [command presentDrawable:drawable];
  [command commit];

  if (report)
  {
    [command waitUntilCompleted];
    if (command.status != MTLCommandBufferStatusCompleted)
    {
      *message = command.error.localizedDescription ?: @"The Metal command did not complete";
      return NO;
    }
    *report = {static_cast<uint32_t>(target.pixelFormat), static_cast<uint32_t>(target.width),
      static_cast<uint32_t>(target.height), 0, 0, 1023, 0, 0};
    bool grayCodes[1024] = {};
    for (NSUInteger y = 0; y < target.height; ++y)
    {
      auto* row = reinterpret_cast<const uint32_t*>(static_cast<const uint8_t*>(readback.contents) + y * rowBytes);
      NSUInteger sourceY = std::min(static_cast<NSUInteger>((target.height - y - 0.5) * height / target.height), height - 1);
      for (NSUInteger x = 0; x < target.width; ++x)
      {
        uint32_t packed = row[x];
        uint32_t gray = packed & 1023;
        if (gray != ((packed >> 10) & 1023) || gray != ((packed >> 20) & 1023))
          ++report->NonGrayPixels;
        grayCodes[gray] = true;
        report->MinCode = gray < report->MinCode ? gray : report->MinCode;
        report->MaxCode = gray > report->MaxCode ? gray : report->MaxCode;
        NSUInteger sourceX = std::min(static_cast<NSUInteger>((x + 0.5) * width / target.width), width - 1);
        auto expected = static_cast<int>(std::lround(1023.0 * std::clamp((pixels[sourceY * width + sourceX] - low) / range, 0.0f, 1.0f)));
        auto distance = static_cast<uint32_t>(std::abs(expected - static_cast<int>(gray)));
        report->MaxSourceCodeError = std::max(report->MaxSourceCodeError, distance);
      }
    }
    for (bool present : grayCodes)
      report->GrayLevels += present;
  }
  return YES;
}
@end

extern "C" void* SlicerMetalSliceAttach(uintptr_t parentId, char* error, size_t size)
{
  @autoreleasepool
  {
    if (![NSThread isMainThread] || !parentId)
    {
      SetError(error, size, @"Attach must be called for a native view on the main thread");
      return nullptr;
    }
    NSString* message = nil;
    NSView* parent = (__bridge NSView*)reinterpret_cast<void*>(parentId);
    auto* surface = [[SlicerMetal10BitSurface alloc] initWithParent:parent error:&message];
    if (!surface)
      SetError(error, size, message);
    return (__bridge_retained void*)surface;
  }
}

extern "C" void SlicerMetalSliceDetach(void* handle)
{
  @autoreleasepool
  {
    SlicerMetal10BitSurface* surface = (__bridge_transfer SlicerMetal10BitSurface*)handle;
    [surface removeFromSuperview];
  }
}

extern "C" int SlicerMetalSliceDraw(void* handle, const uint16_t* pixels, unsigned width, unsigned height,
  float low, float range, PixelReport* report, char* error, size_t size)
{
  @autoreleasepool
  {
    auto* surface = (__bridge SlicerMetal10BitSurface*)handle;
    NSString* message = nil;
    BOOL ok = [surface render:pixels width:width height:height low:low range:range rampTest:NO report:report error:&message];
    if (!ok)
      SetError(error, size, message);
    return ok;
  }
}

extern "C" int SlicerMetalSliceRamp(void* handle, PixelReport* report, char* error, size_t size)
{
  @autoreleasepool
  {
    auto* surface = (__bridge SlicerMetal10BitSurface*)handle;
    uint16_t pixels[1024];
    for (unsigned i = 0; i < 1024; ++i)
      pixels[i] = static_cast<uint16_t>(round(65535.0 * i / 1023.0));
    NSString* message = nil;
    BOOL ok = [surface render:pixels width:1024 height:1 low:0 range:65535 rampTest:YES report:report error:&message];
    if (!ok)
      SetError(error, size, message);
    return ok;
  }
}
