"""machine d'etats thread-safe pour le cycle de vie d'ava."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum
import threading
import time
from typing import Callable


class AvaState(str, Enum):
    DORMANT = "dormant"
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    ACTING = "action"
    SPEAKING = "speaking"
    BOOTING = "booting"
    ERROR = "error"


@dataclass(frozen=True)
class StateSnapshot:
    state: AvaState
    label: str
    revision: int
    changed_at: float


class InvalidTransition(RuntimeError):
    pass


_ALLOWED = {
    AvaState.DORMANT: {
        AvaState.IDLE, AvaState.LISTENING, AvaState.SPEAKING,
        AvaState.BOOTING, AvaState.ERROR,
    },
    AvaState.IDLE: {
        AvaState.DORMANT, AvaState.LISTENING, AvaState.THINKING,
        AvaState.SPEAKING, AvaState.BOOTING, AvaState.ERROR,
    },
    AvaState.LISTENING: {
        AvaState.IDLE, AvaState.THINKING, AvaState.SPEAKING,
        AvaState.DORMANT, AvaState.ERROR,
    },
    AvaState.THINKING: {
        AvaState.ACTING, AvaState.SPEAKING, AvaState.LISTENING,
        AvaState.IDLE, AvaState.DORMANT, AvaState.ERROR,
    },
    AvaState.ACTING: {
        AvaState.IDLE, AvaState.SPEAKING, AvaState.LISTENING,
        AvaState.DORMANT, AvaState.ERROR,
    },
    AvaState.SPEAKING: {
        AvaState.IDLE, AvaState.LISTENING, AvaState.THINKING,
        AvaState.DORMANT, AvaState.ERROR,
    },
    AvaState.BOOTING: {
        AvaState.IDLE, AvaState.SPEAKING, AvaState.DORMANT, AvaState.ERROR,
    },
    AvaState.ERROR: {AvaState.IDLE, AvaState.DORMANT, AvaState.LISTENING},
}


class AssistantStateMachine:
    """source de verite unique pour l'etat visible et fonctionnel d'ava."""

    def __init__(
        self,
        on_change: Callable[[StateSnapshot], None] | None = None,
        history_size: int = 64,
    ) -> None:
        self._lock = threading.RLock()
        self._on_change = on_change
        self._snapshot = StateSnapshot(AvaState.DORMANT, "", 0, time.time())
        self._history: deque[StateSnapshot] = deque(
            [self._snapshot], maxlen=history_size,
        )

    @property
    def snapshot(self) -> StateSnapshot:
        with self._lock:
            return self._snapshot

    @property
    def history(self) -> tuple[StateSnapshot, ...]:
        with self._lock:
            return tuple(self._history)

    def transition(
        self,
        state: AvaState | str,
        label: str = "",
        *,
        force: bool = False,
    ) -> StateSnapshot:
        target = state if isinstance(state, AvaState) else AvaState(state)
        callback = None
        with self._lock:
            current = self._snapshot.state
            if target != current and not force and target not in _ALLOWED[current]:
                raise InvalidTransition(f"transition interdite : {current} -> {target}")
            snapshot = StateSnapshot(
                target,
                label,
                self._snapshot.revision + 1,
                time.time(),
            )
            self._snapshot = snapshot
            self._history.append(snapshot)
            callback = self._on_change
        if callback is not None:
            callback(snapshot)
        return snapshot

    def dormant(self) -> StateSnapshot:
        return self.transition(AvaState.DORMANT, "", force=True)

    def idle(self, label: str = "Prete") -> StateSnapshot:
        return self.transition(AvaState.IDLE, label, force=True)
