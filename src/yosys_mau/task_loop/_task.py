from __future__ import annotations

import asyncio
import dataclasses
import functools
import gc
import inspect
import signal
import typing
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from itertools import count
from typing import Any, Awaitable, Callable, Iterator, Literal

from typing_extensions import ParamSpec, Self

from yosys_mau.stable_set import StableSet

from . import job_server as job

T = typing.TypeVar("T")
T_TaskEvent = typing.TypeVar("T_TaskEvent", bound="TaskEvent")
Args = ParamSpec("Args")

_current_task: ContextVar[Task] = ContextVar(f"{__name__}._current_task")


@contextmanager
def set_current_task(task: Task) -> typing.Iterator[None]:
    token = _current_task.set(task)
    try:
        yield
    finally:
        _current_task.reset(token)


class TaskLoopError(RuntimeError):
    """Raised when the task loop is in an invalid state."""

    pass


def current_task() -> Task:
    try:
        return _current_task.get()
    except LookupError:
        raise TaskLoopError("no task is currently active") from None


def current_task_or_none() -> Task | None:
    try:
        return _current_task.get()
    except LookupError:
        pass


def task_loop() -> TaskLoop:
    if global_task_loop is None:
        raise TaskLoopError("no task loop is currently active")
    return global_task_loop


def root_task() -> RootTask:
    return task_loop().root_task


TaskState = Literal[
    "preparing", "pending", "running", "waiting", "done", "cancelled", "discarded", "failed"
]


def as_awaitable(fn: Callable[Args, T | Awaitable[T]]) -> Callable[Args, Awaitable[T]]:
    def wrapper(*args: Args.args, **kwargs: Args.kwargs) -> Awaitable[T]:
        result = fn(*args, **kwargs)
        if not isinstance(result, Awaitable):

            async def wrapper() -> T:
                return result

            return wrapper()
        return result  # type: ignore

    return wrapper


global_task_loop: TaskLoop | None = None


class TaskLoop:
    root_task: RootTask
    task_eq_ids: Iterator[int]

    def __init__(
        self,
        on_run: Callable[[], None | Awaitable[None]],
        *,
        handle_sigint: bool = True,
    ) -> None:
        global global_task_loop
        if global_task_loop is not None:
            raise TaskLoopError("a task loop is already installed")
        global_task_loop = self

        async def wrapper():
            if handle_sigint:
                asyncio.get_event_loop().add_signal_handler(signal.SIGINT, self._handle_sigint)
            job.global_client()  # early setup of the job server client

            RootTask(on_run=on_run)
            self.root_task.name = "root"

            await self.root_task.finished

            # Some __del__ implementations in the stdlib expect the event loop to be still running
            # and cause ignored exception warnings when they are cycle collected after the event
            # loop exited. Manually triggering a cycle collection fixes this.
            gc.collect()

        try:
            asyncio.run(wrapper())
        finally:
            global_task_loop = None

    def _handle_sigint(self) -> None:
        if self.root_task:
            self.root_task.cancel()


def run_task_loop(
    on_run: Callable[[], None | Awaitable[None]], *, handle_sigint: bool = True
) -> None:
    """Run the task loop.

    :param on_run: The function (async or sync) to run in the context of the task loop's root task.
    :param handle_sigint: Whether to handle SIGINT (Ctrl+C) by cancelling the root task (recursively
        cancelling all child tasks).

    """
    TaskLoop(on_run, handle_sigint=handle_sigint)


class Task:
    """Base class for all tasks.

    To customize the functionality performed by a task, either declare a subclass of `Task`
    overriding the methods starting with ``on_`` or use the `on_run` and `on_prepare` arguments of
    the constructor.
    """

    __name: str
    __parent: Task | None
    __children: StableSet[Task]
    __child_names: set[str]

    __dependencies: StableSet[Task]
    __pending_dependencies: dict[Task, Callable[..., Any]]
    __pending_children: dict[Task, Callable[..., Any]]

    __reverse_dependencies: StableSet[Task]

    __error_handlers: dict[Task | None, Callable[[BaseException], Awaitable[None] | None]]

    __state: TaskState

    __aio_main_task: asyncio.Task[None]
    __aio_background_tasks: StableSet[asyncio.Task[None]]
    __aio_wait_background_tasks: StableSet[asyncio.Task[None]]

    __started: asyncio.Future[None]
    __finished: asyncio.Future[None]

    __event_cursors: dict[type, asyncio.Future[TaskEventCursor[Any]]]

    __use_lease: bool
    __lease: job.Lease | None

    __cancelled_by: Task | None

    discard: bool
    """If set to, the task will be discarded (automatically cancelled) when the last of the
    tasks depending on it finishes (by failure or cancellation).

    Defaults to `True`.
    """

    @property
    def use_lease(self) -> bool:
        """Whether the task should obtain a lease from the job server before running."""
        return self.__use_lease

    @use_lease.setter
    def use_lease(self, use_lease: bool) -> None:
        if use_lease and not self.__use_lease:
            assert (
                self.__state == "preparing"
            ), "cannot change lease usage after the task is prepared"
        self.__use_lease = use_lease
        if not use_lease:
            self.__lease = None

    @property
    def parent(self) -> Task | None:
        """The parent task of this task, or `None` if this is the root task."""
        return self.__parent

    @property
    def state(self) -> TaskState:
        """The current state of the task.

        .. todo:: diagram of the possible state transitions


        """
        return self.__state

    @property
    def name(self) -> str:
        """Name of this task.

        By default the class name is used, if necessary made unique among sibling tasks by appending
        a number.

        Subclasses can assign a more meaningful name in their constructor.
        """
        return self.__name

    @name.setter
    def name(self, name: str) -> None:
        assert name
        if self.parent is None:
            self.__name = name
            return

        if self.__name:
            self.parent.__child_names.remove(self.__name)

        if name not in self.parent.__child_names:
            self.__name = name
            self.parent.__child_names.add(name)
            return

        for i in count(1):
            if (unique_name := f"{name}#{i}") not in self.parent.__child_names:
                self.__name = unique_name
                self.parent.__child_names.add(unique_name)
                break

    @property
    def path(self) -> str:
        """The path of this task in the task tree.

        Lists the names of the path from the containing top-level task to this task, separated by
        dots.
        """
        if self.parent and self.parent.parent:
            return f"{self.parent.path}.{self.name}"
        return self.name

    def __str__(self) -> str:
        return self.path

    def __init__(
        self,
        on_run: Callable[[Self], Awaitable[None] | None]
        | Callable[[], Awaitable[None] | None]
        | None = None,
        *,
        on_prepare: Callable[[Self], Awaitable[None] | None]
        | Callable[[], Awaitable[None] | None]
        | None = None,
    ):
        """The constructor creates a new task as child task of the current task and schedule it to
        run in the current ask loop.

        :param on_run: The function to call when the task is run. Specifying this is an alternative
            to subclassing `Task` and overriding `on_run`.
        :param on_prepare: The function to call when the task is prepared. Specifying this is an
            alternative to subclassing `Task` and overriding `on_prepare`.
        """
        if on_run is not None:
            if inspect.signature(on_run).parameters:
                on_run = functools.partial(on_run, self)
            self.on_run = as_awaitable(on_run)  # type: ignore
        if on_prepare is not None:
            if inspect.signature(on_prepare).parameters:
                on_prepare = functools.partial(on_prepare, self)
            self.on_prepare = as_awaitable(on_prepare)  # type: ignore

        self.__name = ""
        self.__state = "preparing"
        self.__children = StableSet()
        self.__child_names = set()
        self.__dependencies = StableSet()
        self.__pending_dependencies = {}
        self.__pending_children = {}
        self.__reverse_dependencies = StableSet()
        self.__error_handlers = {}
        self.__started = asyncio.Future()
        self.__finished = asyncio.Future()
        self.__use_lease = False
        self.__lease = None
        self.__cleaned_up = False
        self.__aio_background_tasks = StableSet()
        self.__aio_wait_background_tasks = StableSet()
        self.__event_cursors = {}
        self.__cancelled_by = None

        self.discard = True

        if isinstance(self, RootTask):
            self.__parent = None
            loop = task_loop()
            assert not hasattr(loop, "root_task")
            task_loop().root_task = self
        else:
            self.__parent = current_task()

            assert (
                self.__parent.state == "running"
            ), "cannot create child tasks before the parent task is running"
            # TODO allow this but make children block for their parent having started

            self.__parent.__add_child(self)

        self.name = self.__class__.__name__

        self.__aio_main_task = asyncio.create_task(self.__task_main())

    def __change_state(self, new_state: TaskState) -> None:
        if self.__state == new_state:
            return
        old_state, self.__state = self.__state, new_state
        if self.__parent:
            with self.as_current_task():
                TaskStateChange(old_state, new_state).emit()

    def depends_on(self, task: Task) -> None:
        """Register a dependency on another task."""
        assert self.state in (
            "preparing",
            "pending",
        ), "cannot add dependencies after task has started"
        self.__dependencies.add(task)
        if task.state in ("preparing", "pending", "running"):
            callback: Callable[[Any], None] = lambda _: self.__dependency_finished(task)
            task.__finished.add_done_callback(callback)
            self.__pending_dependencies[task] = callback
            task.__reverse_dependencies.add(self)

    def set_error_handler(
        self, task: Task | None, handler: Callable[[BaseException], Awaitable[None] | None]
    ) -> None:
        """Register a handler for failing or cancelled tasks.

        A registered error handler stops this task from automatically failing (or becoming
        cancelled) when a child or dependency fails (or is cancelled).

        :param task: The task to register the handler for. If `None`, the handler is registered as a
            fallback.

        :param handler: The handler to call when the task fails or is cancelled. The handler is
            invoked as a background coroutine in the context of this task. The failed task can be
            recovered from the `TaskAborted` exception.

        """
        self.__error_handlers[task] = handler

    def __add_child(self, task: Task) -> None:
        assert self.state == "running", "children can only be added to a running tasks"
        self.__children.add(task)
        if task.state in ("preparing", "pending", "running"):
            callback: Callable[[Any], None] = lambda _: self.__child_finished(task)
            task.__finished.add_done_callback(callback)
            self.__pending_children[task] = callback

    def __dependency_finished(self, task: Task) -> None:
        self.__pending_dependencies.pop(task)
        self.__propagate_failure(task, wrap=(DependencyFailed, DependencyCancelled))
        self.__check_start()

    def __child_finished(self, task: Task) -> None:
        self.__pending_children.pop(task)
        self.__propagate_failure(task, wrap=(ChildFailed, ChildCancelled))
        self.__check_finish()

    def __check_start(self) -> None:
        if self.state != "pending":
            return
        if self.__pending_dependencies:
            self.__lease = None
            return
        if self.__use_lease:
            # TODO wrap the raw lease in some logic that prefers passing leases within the hierarchy
            # before returning them to the job server
            if self.__lease is None:
                self.__lease = job.global_client().request_lease()
            if not self.__lease.ready:
                self.__lease.add_ready_callback(self.__check_start)
                return
        self.__started.set_result(None)

    def __check_finish(self) -> None:
        if self.state != "waiting":
            return
        if self.__pending_children:
            return
        if self.__aio_wait_background_tasks:
            return
        self.__cleanup()
        self.__finished.set_result(None)

    def __propagate_failure(
        self,
        task: Task,
        exception: BaseException | None = None,
        *,
        wrap: tuple[Callable[[Task], BaseException], Callable[[Task], BaseException]] | None = None,
    ) -> None:
        if exception is None:
            try:
                exception = task.__finished.exception()
            except asyncio.CancelledError as exc:
                exception = exc

            if exception is None:
                return

        if wrap:
            wrap_failed, wrap_cancelled = wrap

            if isinstance(exception, asyncio.CancelledError):
                exc = wrap_cancelled(task)
            else:
                exc = wrap_failed(task)
                exc.__cause__ = exception
            exception = exc

        if handler := self.__error_handlers.get(task):
            self.background(lambda: handler(exception), wait=True, error_handler=True)
            return

        if handler := self.__error_handlers.get(None):
            self.background(lambda: handler(exception), wait=True, error_handler=True)
            return

        if isinstance(exception, asyncio.CancelledError):
            asyncio.get_event_loop().call_soon(self.__discard_via, task)
        else:
            self.__failed(exception)

    async def __task_main(self) -> None:
        __prev_task = _current_task.set(self)
        try:
            TaskStateChange(None, self.__state).emit()
            await self.on_prepare()
            self.__change_state("pending")
            self.__check_start()
            await self.started
            self.__change_state("running")
            await self.on_run()
            self.__lease = None
            self.__change_state("waiting")
            self.__check_finish()
            await self.finished
            self.__change_state("done")
        except Exception as exc:
            self.__propagate_failure(self, exc)
        finally:
            _current_task.reset(__prev_task)
            self.__cleanup()

    def __cleanup(self):
        if self.__cleaned_up:
            return
        self.__cleaned_up = True
        self.on_cleanup()
        for task, callback in self.__pending_children.items():
            task.__finished.remove_done_callback(callback)

        for task, callback in self.__pending_dependencies.items():
            task.__finished.remove_done_callback(callback)
            task.__reverse_dependencies.remove(self)
            if not task.__reverse_dependencies and task.discard:
                asyncio.get_event_loop().call_soon(lambda: task.__cancel(discard=True))

        for aio_task in self.__aio_background_tasks:
            aio_task.cancel()

        for aio_task in self.__aio_wait_background_tasks:
            aio_task.cancel()

        for cursor in self.__event_cursors.values():
            cursor.cancel()

    def __failed(self, exc: BaseException | None) -> None:
        if exc is None or self.is_finished:
            return
        self.__lease = None
        if not self.__started.done():
            self.__started.set_exception(exc)
            self.__started.exception()
        if not self.__finished.done():
            self.__finished.set_exception(exc)
            self.__finished.exception()
        self.__change_state("failed")

        for child in self.__children:
            child.__cancel(discard=True)

    async def on_prepare(self) -> None:
        """Actions to perform right after the task is created, before scheduling it to run.

        Can be used to add dependencies or change other task properties.

        Scheduling the task is delated until this async method returns.

        This executes with ``self`` as the current task.
        """
        pass

    async def on_run(self) -> None:
        """Actions to perform when the task is running.

        This executes with ``self`` as the current task.

        For the task to successfully finish, this async method must return, but child tasks can
        delay this further.

        """
        pass

    @property
    async def started(self) -> None:
        """Awaitable that resolves when the task has started running."""
        try:
            await asyncio.shield(self.__started)
        except asyncio.CancelledError:
            raise TaskCancelled(self) from None
        except BaseException as exc:
            raise TaskFailed(self) from exc

    @property
    async def finished(self) -> None:
        """Awaitable that resolves when the task has finished running.

        This includes successful completion, cancellations and failure.
        """
        try:
            await asyncio.shield(self.__finished)
        except asyncio.CancelledError:
            raise TaskCancelled(self) from None
        except BaseException as exc:
            raise TaskFailed(self) from exc

    @property
    def is_finished(self) -> bool:
        """Whether the task has finished running.

        This includes successful completion, cancellations and failure.
        """
        return self.__state in ("done", "cancelled", "discarded", "failed")

    @property
    def is_done(self) -> bool:
        """Whether the task has finished running successfully."""
        return self.__state == "done"

    @property
    def is_aborted(self) -> bool:
        """Indicates that the task failed or was cancelled."""
        return self.__state in ("cancelled", "discarded", "failed")

    def cancel(self) -> None:
        """Cancel the task.

        This will also cancel all pending children as well as tasks that depend on this one and do
        not explicitly handle cancellation of their dependencies (with the exception of the current
        task).
        """
        self.__cancelled_by = current_task()
        self.__cancel(discard=False)

    def __discard_via(self, task: Task) -> None:
        if task.__cancelled_by is self:
            return
        self.__cancel(discard=True)

    def __cancel(self, discard: bool = False) -> None:
        if self.is_finished:
            return
        self.__aio_main_task.cancel()
        self.__lease = None
        if not self.__started.done():
            self.__started.cancel()
        if not self.__finished.done():
            self.__finished.cancel()

        self.__change_state("discarded" if discard else "cancelled")

        for child in self.__children:
            child.__cancel(discard=discard)

        with self.as_current_task():
            self.on_cancel()

    def on_cancel(self):
        """Actions to perform when the task is cancelled.

        This runs when the task is cancelled (either directly or via a parent), but not when a
        dependency was cancelled. See `on_cleanup` for an alternative that runs in all cases.
        """
        pass

    def on_cleanup(self):
        """Actions to perform after the task finished.

        This includes successful completion, cancellations and failure.
        """
        pass

    def background(
        self,
        target: Callable[[], Awaitable[None] | None],
        *,
        wait: bool = False,
        error_handler: bool = False,
    ) -> asyncio.Task[None]:
        """Run a background coroutine in the context of this task.

        The coroutine will execute with the current task set to this task. When the task fails or is
        cancelled, the background coroutine will be cancelled as well.

        :param target: The coroutine to run.
        :param wait: Whether to wait for the background coroutine to finish before letting the task
            finish.
        :param error_handler: Whether the background coroutine is an error handler. Without setting
            this, it is an error to install a background handler for a finished task.

        :return: The asyncio task (not a task loop task) that runs the background coroutine. Can be
            used to cancel the background coroutine.
        """
        assert error_handler or self.state in (
            "running",
            "waiting",
        ), "background handlers can only be created for running or waiting tasks"
        target_coroutine = as_awaitable(target)

        aio_task = None

        if error_handler and self.is_finished:
            wait = False

        async def wrapper():
            nonlocal aio_task
            __prev_task = _current_task.set(self)
            try:
                await target_coroutine()
            except asyncio.CancelledError:
                pass
            except BaseException as e:
                self.__failed(e)
            finally:
                _current_task.reset(__prev_task)
                if aio_task is not None:
                    if wait:
                        self.__aio_wait_background_tasks.remove(aio_task)
                        self.__check_finish()
                    else:
                        self.__aio_background_tasks.discard(aio_task)

        aio_task = asyncio.create_task(wrapper())

        if error_handler and self.is_finished:
            return aio_task

        if wait:
            self.__aio_wait_background_tasks.add(aio_task)
        else:
            self.__aio_background_tasks.add(aio_task)

        return aio_task

    def __emit_event__(self, event: TaskEvent) -> None:
        assert event.source is self

        current = self

        while current is not None:
            for mro_item in type(event).mro():
                cursor = current.__event_cursors.get(mro_item)
                if cursor is None:
                    continue

                next_cursor: asyncio.Future[TaskEventCursor[Any]] = asyncio.Future()
                cursor.set_result(TaskEventCursor(event, next_cursor))
                current.__event_cursors[mro_item] = next_cursor

            current = current.__parent

    def events(
        self, event_type: type[T_TaskEvent], where: Callable[[T_TaskEvent], bool] | None = None
    ) -> TaskEventStream[T_TaskEvent]:
        """Return an async iterator that yields events emitted by this task or its children.

        :param event_type: The type of events to yield, use `TaskEvent` to yield all events.
        :param where: A predicate that filters the events to yield.

        Note that using ``event_type`` is more efficient than a ``where`` predicate that uses
        `isinstance`.
        """
        if event_type not in self.__event_cursors:
            self.__event_cursors[event_type] = asyncio.Future()
        cursor = self.__event_cursors[event_type]
        return TaskEventStream(cursor, where or (lambda _: True))

    def as_current_task(self) -> typing.ContextManager[None]:
        """Returns a context manager that temporarily overrides the current task.

        This is safe to use with concurrently executing tasks as each execution context has its own
        current task.
        """
        return set_current_task(self)


class RootTask(Task):
    pass


class TaskAborted(Exception):
    """Base class for exceptions caused by a task being aborted.

    This includes failure and cancellation.
    """

    task: Task
    """The affected task."""

    def __init__(self, task: Task):
        self.task = task


class TaskFailed(TaskAborted):
    """Exception caused by a task failing."""

    def __init__(self, task: Task):
        self.task = task

    def __str__(self) -> str:
        return f"Task {self.task} failed"


class TaskCancelled(TaskAborted, asyncio.CancelledError):
    """Exception caused by a task being cancelled."""

    def __str__(self) -> str:
        return f"Task {self.task} cancelled"


class DependencyAborted(TaskAborted):
    """Base class for exceptions caused by a dependency being aborted."""

    pass


class DependencyFailed(DependencyAborted, TaskFailed):
    """Exception caused by a dependency failing."""

    def __str__(self) -> str:
        return f"Dependency {self.task} failed"


class DependencyCancelled(DependencyAborted, TaskCancelled):
    """Exception caused by a dependency being cancelled."""

    def __str__(self) -> str:
        return f"Dependency {self.task} cancelled"


class ChildAborted(TaskAborted):
    """Base class for exceptions caused by a child task being aborted."""

    pass


class ChildFailed(ChildAborted, TaskFailed):
    """Exception caused by a child task failing."""

    def __str__(self) -> str:
        if self.task.parent and self.task.parent.parent:
            return f"Child task {self.task} failed"
        else:
            return f"Top-level task {self.task} failed"


class ChildCancelled(ChildAborted, TaskCancelled):
    """Exception caused by a child task being cancelled."""

    def __str__(self) -> str:
        if self.task.parent and self.task.parent.parent:
            return f"Child task {self.task} cancelled"
        else:
            return f"Top-level task {self.task} cancelled"


@dataclass
class TaskEvent:
    """Base class for events emitted by tasks.

    Note that the source of the event is recorded at the time of construction, not when the event is
    emitted. To emit an event from a different task, create the event inside a
    `Task.as_current_task` block.
    """

    def __post_init__(self) -> None:
        self.__source = current_task()

    def __init_subclass__(cls) -> None:
        # This is a hack that prevents dataclasses from adding a __repr__ method without requiring
        # the ``repr=false`` argument to `dataclass`.
        cls.__repr__ = cls.__repr__

    def __repr__(self):
        out: list[str] = []
        for field in dataclasses.fields(self):
            out.append(f"{field.name}={getattr(self, field.name)!r}")
        return f"{self.source}: {self.__class__.__qualname__}({', '.join(out)})"

    @property
    def source(self) -> Task:
        """The task that created and emitted this event."""
        return self.__source

    def emit(self) -> None:
        """Emit this event from the current task.

        It is an error to emit an event from a task other than the one that created it. To emit
        events from a different task, create the event inside a `Task.as_current_task` block.
        """
        self.source.__emit_event__(self)


@dataclass
class TaskEventCursor(typing.Generic[T_TaskEvent]):
    event: T_TaskEvent
    tail: asyncio.Future[TaskEventCursor[T_TaskEvent]]


class TaskEventStream(typing.AsyncIterator[T_TaskEvent]):
    """An async iterator that yields events emitted by a task or its children."""

    __cursor: asyncio.Future[TaskEventCursor[T_TaskEvent]]
    __where: Callable[[T_TaskEvent], bool]

    def __init__(
        self,
        cursor: asyncio.Future[TaskEventCursor[T_TaskEvent]],
        where: Callable[[T_TaskEvent], bool],
    ):
        self.__cursor = cursor
        self.__where = where

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> T_TaskEvent:
        while True:
            try:
                cursor = await self.__cursor
            except asyncio.CancelledError:
                raise StopAsyncIteration
            result = cursor.event
            self.__cursor = cursor.tail
            if self.__where(result):
                return result

    def process(
        self,
        handler: Callable[[Self], Awaitable[None]],
        *,
        wait: bool = True,
    ) -> None:
        """Process events from this event stream using a background coroutine of the current task.

        :param handler: A n async function that receives this event stream as argument.
        :param wait: Forwarded to `Task.background`. If set (the default), it will prevent the
            current task from finishing until the stream is exhausted.
        """
        current_task().background(lambda: handler(self), wait=wait)

    def handle(self, handler: Callable[[T_TaskEvent], Awaitable[None] | None]) -> None:
        """Handle events from this event stream using a callback function.

        The callback is executed in the context of the current task. Events emitted after the
        current task finished do not invoke the callback. Note that an async handler can block the
        processing of subsequent events.

        :param handler: An async or sync function that receives each event as argument.
        """
        handler = as_awaitable(handler)

        async def stream_handler():
            async for event in self:
                await handler(event)

        current_task().background(stream_handler, wait=False)


class DebugEvent(TaskEvent):
    """Base class for debug events emitted by the task loop itself."""

    pass


@dataclass(repr=False)
class TaskStateChange(DebugEvent):
    """Event emitted whenever a task changes state."""

    previous_state: TaskState | None
    state: TaskState

    def __repr__(self) -> str:
        return f"{self.source}: {self.previous_state} -> {self.state}"
