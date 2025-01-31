from __future__ import annotations

import types
from typing import Any, Generic, TypeVar
from weakref import WeakKeyDictionary

from ._task import Task, current_task_or_none

T = TypeVar("T")


class _MISSING_TYPE:
    pass


MISSING = _MISSING_TYPE()


class TaskContextDescriptor(Generic[T]):
    """A descriptor that stores a value per `Task`.

    When assigning or deleting the attribute from within the task loop, this affects the value
    stored for the current task.

    With no task loop running, this affects the default value for the attribute.

    When reading the attribute, this performs a lookup in the task hierarchy, starting from the
    current task and going up to the root task. If no value is found the default value is returned.
    """

    __data: WeakKeyDictionary[Task, T]
    __default: T
    __owner: Any
    __name: str | None

    def __init__(self, default: T | _MISSING_TYPE = MISSING) -> None:
        self.__data = WeakKeyDictionary()
        if default is not MISSING:
            self.default = default  # type: ignore
        self.__owner = None
        self.__name = None

    def __set_name__(self, owner: type, name: str) -> None:
        self.__owner = owner
        self.__name = name

    def __attr_name(self) -> str:
        if self.__name is None:
            return repr(self)
        else:
            return f"{self.__owner.__qualname__}.{self.__name}"

    def __get__(self, instance: Any, owner: type) -> T:
        cursor = current_task_or_none()
        while cursor is not None:
            try:
                return self.__data[cursor]
            except KeyError:
                cursor = cursor.parent
        try:
            return self.default
        except AttributeError:
            raise AttributeError(f"Context variable {self.__attr_name()} not set") from None

    def __set__(self, instance: Any, value: T) -> None:
        task = current_task_or_none()
        if task is None:
            self.default = value
        else:
            self.__data[task] = value

    def __delete__(self, instance: Any) -> None:
        task = current_task_or_none()
        if task is None:
            try:
                del self.default
            except KeyError:
                raise AttributeError(
                    f"Context variable {self.__attr_name()} not set for the current task"
                ) from None
        else:
            del self.__data[task]

    @property
    def default(self) -> T:
        """The default value for this context variable.

        It is possible to subclass this descriptor and to override this property to provide a custom
        dynamic behavior for the default value while retaining the same lookup and assignment
        behavior for values associated to tasks.
        """
        return self.__default

    @default.setter
    def default(self, value: T) -> None:
        self.__default = value

    @default.deleter
    def default(self) -> None:
        del self.__default


class InlineContextVar(Generic[T]):
    """A descriptor that makes a task context variable available as an attribute of a task.

    This has to be used within subclasses of `Task`. Accessing the corresponding attribute of a
    `Task` instance behaves as if that task was the current task and the task context variable was
    accessed.

    .. todo:: Example to illustrate the previous paragraph.

    :param context: The task context variable group that contains the task context variable.
    :param name: The name of the task context variable within this group.
    """

    def __init__(self, context_var_group: Any, name: str):
        self.__context = context_var_group
        self.__name = name

    def __get__(self, instance: Any, owner: type) -> T:
        with instance.as_current_task():
            return getattr(self.__context, self.__name)

    def __set__(self, instance: Any, value: T) -> None:
        with instance.as_current_task():
            setattr(self.__context, self.__name, value)

    def __delete__(self, instance: Any) -> None:
        with instance.as_current_task():
            delattr(self.__context, self.__name)


def task_context_class(cls: type[T]) -> type[T]:
    for name in getattr(cls, "__annotations__", ()):
        try:
            default_or_descriptor = cls.__dict__[name]
        except KeyError:
            setattr(cls, name, TaskContextDescriptor())
        else:
            if isinstance(default_or_descriptor, types.FunctionType) or not hasattr(
                default_or_descriptor, "__get__"
            ):
                setattr(cls, name, TaskContextDescriptor(default_or_descriptor))
    return cls


def task_context(cls: type[T]) -> T:
    """Decorator for a class defining a group of task context variables.

    Note that this decorator replaces the class with a singleton instance of the class with all
    annotated non-descriptor attributes wrapped in a `TaskContextDescriptor`. Existing
    non-descriptor attribute values are used as default values for the new descriptor.

    .. todo:: Example for `task_context`
    """

    cls = task_context_class(cls)

    # This below is needed to make Sphinx happy, otherwise we could just return ``cls()``.
    class AsMetaclass(cls, type):  # type: ignore
        pass

    class AsInstance(metaclass=AsMetaclass):
        pass

    if hasattr(cls, "__module__"):
        AsInstance.__module__ = cls.__module__
    if hasattr(cls, "__name__"):
        AsInstance.__name__ = cls.__name__
    if hasattr(cls, "__qualname__"):
        AsInstance.__qualname__ = cls.__qualname__
    if hasattr(cls, "__doc__"):
        AsInstance.__doc__ = cls.__doc__

    return AsInstance  # type: ignore
