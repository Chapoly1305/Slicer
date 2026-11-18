"""
Client sessions for the Slicer web server.

Several independent clients (for example multiple AI agents connected through
an MCP bridge) may talk to the same Slicer instance at the same time. Without
sessions they share a single Python namespace for ``/exec`` and can clear or
delete each other's nodes. A session gives each client:

- its own Python namespace for ``/exec`` (variables do not leak between clients),
- ownership tracking of the MRML nodes it creates (nodes are tagged with the
  ``WebServer.SessionID`` node attribute),
- scoped destructive operations (deleting "everything" only removes the nodes
  owned by the session, never the whole scene).

A request is associated with a session by adding the ``session=<id>`` query
parameter. Sessions are created implicitly on first use, or explicitly with
``POST /slicer/session``.

All clients still share one application: one MRML scene, one layout and one
set of views. Sessions prevent accidental interference; they are not a
security boundary (code run through ``/exec`` can still access everything).
"""

import builtins
import contextlib
import json
import logging
import os
import re
import time
import urllib
import uuid

import qt
import vtk

import slicer
from slicer.util import LazyImport

logger = logging.getLogger(__name__)

SESSION_QUERY_PARAMETER = "session"
"""Query parameter that selects the session of a request."""

SCOPE_QUERY_PARAMETER = "scope"
"""Query parameter that selects which nodes a request operates on (``session`` or ``all``)."""

SESSION_NODE_ATTRIBUTE = "WebServer.SessionID"
"""Node attribute that stores the ID of the session that created the node."""

_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


def isValidSessionId(sessionId: str) -> bool:
    return bool(sessionId) and _SESSION_ID_PATTERN.match(sessionId) is not None


class WebServerSession:
    """State of a single client session."""

    def __init__(self, sessionId: str, name: str | None = None):
        self.id = sessionId
        self.name = name or sessionId
        self.created = time.time()
        self.lastAccess = self.created
        self.requestCount = 0
        self.namespace = self._createNamespace()

    def _createNamespace(self) -> dict:
        """Python globals used for ``/exec`` requests of this session.

        Contains the same convenience imports that are available to
        non-session ``/exec`` requests.
        """
        import ctk

        return {
            "__name__": f"__webserver_session_{self.id}__",
            "__builtins__": builtins,
            "__session__": self,
            "slicer": slicer,
            "vtk": vtk,
            "qt": qt,
            "ctk": ctk,
            "json": json,
            "logging": logging,
            "os": os,
            "time": time,
            "urllib": urllib,
            "numpy": LazyImport("numpy"),
        }

    def touch(self):
        self.lastAccess = time.time()
        self.requestCount += 1

    def nodes(self) -> list:
        """Nodes in the scene that are owned by this session."""
        return [node for node in slicer.util.getNodesByClass("vtkMRMLNode")
                if node.GetAttribute(SESSION_NODE_ATTRIBUTE) == self.id]

    def nodeIDs(self) -> list[str]:
        return [node.GetID() for node in self.nodes()]

    def claimNode(self, node):
        """Mark an existing node as owned by this session."""
        node.SetAttribute(SESSION_NODE_ATTRIBUTE, self.id)

    def removeNodes(self) -> list[str]:
        """Remove all nodes owned by this session from the scene."""
        removedNodeIDs = []
        for nodeID in self.nodeIDs():
            # Removing a node may already have removed its display/storage nodes
            node = slicer.mrmlScene.GetNodeByID(nodeID)
            if node is None:
                continue
            slicer.mrmlScene.RemoveNode(node)
            removedNodeIDs.append(nodeID)
        return removedNodeIDs

    def info(self, includeNodes: bool = False) -> dict:
        info = {
            "id": self.id,
            "name": self.name,
            "created": self.created,
            "lastAccess": self.lastAccess,
            "requestCount": self.requestCount,
            "variables": sorted(key for key in self.namespace if not key.startswith("__")),
        }
        if includeNodes:
            info["nodeIDs"] = self.nodeIDs()
        else:
            info["nodeCount"] = len(self.nodeIDs())
        return info


class SessionManager:
    """Registry of web server sessions.

    A single shared instance (see ``sessionManager()``) is used so that
    sessions survive restarting the server or re-creating request handlers.
    """

    def __init__(self):
        self.sessions: dict[str, WebServerSession] = {}
        # Stack of sessions that are currently processing a request.
        # A stack is needed because a request (e.g., /exec that calls
        # slicer.app.processEvents()) may let another request run nested.
        self._activeSessions: list[WebServerSession] = []
        self._sceneObserverTag = None

    # Registry

    def createSession(self, sessionId: str | None = None, name: str | None = None) -> WebServerSession:
        if sessionId is None:
            sessionId = uuid.uuid4().hex[:12]
        if not isValidSessionId(sessionId):
            raise ValueError(f"Invalid session id '{sessionId}': use 1-64 characters of A-Z, a-z, 0-9, '_', '.', '-'")
        if sessionId in self.sessions:
            raise ValueError(f"Session '{sessionId}' already exists")
        session = WebServerSession(sessionId, name)
        self.sessions[sessionId] = session
        logger.info(f"Created web server session {sessionId}")
        return session

    def getSession(self, sessionId: str, create: bool = True) -> WebServerSession | None:
        session = self.sessions.get(sessionId)
        if session is None and create:
            session = self.createSession(sessionId)
        return session

    def closeSession(self, sessionId: str, removeNodes: bool = True) -> dict:
        session = self.sessions.pop(sessionId, None)
        if session is None:
            raise KeyError(f"Session '{sessionId}' does not exist")
        removedNodeIDs = session.removeNodes() if removeNodes else []
        if not removeNodes:
            # Keep the nodes, but release ownership so that they are not
            # attributed to a session that no longer exists.
            for node in session.nodes():
                node.RemoveAttribute(SESSION_NODE_ATTRIBUTE)
        session.namespace.clear()
        logger.info(f"Closed web server session {sessionId}")
        return {"success": True, "id": sessionId, "removedNodeIDs": removedNodeIDs}

    def listSessions(self) -> list[dict]:
        return [session.info() for session in self.sessions.values()]

    # Request scope

    @property
    def activeSession(self) -> WebServerSession | None:
        return self._activeSessions[-1] if self._activeSessions else None

    @contextlib.contextmanager
    def activate(self, session: WebServerSession | None):
        """Process a request on behalf of ``session``.

        Nodes added to the scene while the session is active are tagged as
        owned by the session. ``None`` means the request is not in any session
        (nodes added are not tagged).
        """
        if session is not None:
            session.touch()
        self._activeSessions.append(session)
        self._updateSceneObserver()
        try:
            yield session
        finally:
            self._activeSessions.pop()
            self._updateSceneObserver()

    def _updateSceneObserver(self):
        needed = any(session is not None for session in self._activeSessions)
        if needed and self._sceneObserverTag is None:
            self._sceneObserverTag = slicer.mrmlScene.AddObserver(slicer.vtkMRMLScene.NodeAddedEvent, self._onNodeAdded)
        elif not needed and self._sceneObserverTag is not None:
            slicer.mrmlScene.RemoveObserver(self._sceneObserverTag)
            self._sceneObserverTag = None

    @vtk.calldata_type(vtk.VTK_OBJECT)
    def _onNodeAdded(self, caller, event, node):
        session = self.activeSession
        if session is None or node is None:
            return
        if node.GetAttribute(SESSION_NODE_ATTRIBUTE):
            # Already owned (e.g., node re-added by undo or scene import)
            return
        session.claimNode(node)


_sessionManager = None


def sessionManager() -> SessionManager:
    """Return the application-wide session manager."""
    global _sessionManager
    if _sessionManager is None:
        _sessionManager = SessionManager()
    return _sessionManager
