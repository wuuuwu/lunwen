from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from paper_reviewer.adapters.persistence.database import (
    create_engine,
    create_session_factory,
    initialize_database,
)


class ApplicationUnitOfWork:
    """Own one database engine for one application-service operation.

    Desktop operations may run on different ``QThread`` event loops.  Keeping
    the engine inside this context prevents an async engine from leaking across
    those loop boundaries while centralising the otherwise repeated cleanup.
    """

    def __init__(self, database_url: str, *, initialize: bool = True) -> None:
        self._database_url = database_url
        self._initialize = initialize
        self.engine: AsyncEngine | None = None
        self.sessions: async_sessionmaker[AsyncSession] | None = None

    async def __aenter__(self) -> ApplicationUnitOfWork:
        engine = create_engine(self._database_url)
        try:
            if self._initialize:
                await initialize_database(engine)
            sessions = create_session_factory(engine)
        except BaseException:
            await engine.dispose()
            raise
        self.engine = engine
        self.sessions = sessions
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        engine = self.engine
        self.engine = None
        self.sessions = None
        if engine is not None:
            await engine.dispose()

    def require_sessions(self) -> async_sessionmaker[AsyncSession]:
        if self.sessions is None:
            raise RuntimeError("application unit of work is not active")
        return self.sessions
