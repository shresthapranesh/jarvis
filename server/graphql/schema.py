"""Schema root — composes Query, Mutation, Subscription from per-domain modules."""

from __future__ import annotations

import strawberry
from strawberry.tools import merge_types

from .mutations.artifact import ArtifactMutation
from .mutations.automation import AutomationMutation
from .mutations.conversation import ConversationMutation
from .mutations.memory import MemoryMutation
from .mutations.notification import NotificationMutation
from .mutations.task_run import TaskRunMutation
from .mutations.workflow import WorkflowMutation
from .queries.artifact import ArtifactQuery
from .queries.automation import AutomationQuery
from .queries.conversation import ConversationQuery
from .queries.memory import MemoryQuery
from .queries.models import ModelsQuery
from .queries.notification import NotificationQuery
from .queries.task_run import TaskRunQuery
from .queries.workflow import WorkflowQuery
from .subscriptions.automation import AutomationSubscription
from .subscriptions.chat import ChatSubscription
from .subscriptions.workflow import WorkflowSubscription

Query = merge_types("Query", (
    ModelsQuery, MemoryQuery, ConversationQuery, ArtifactQuery,
    AutomationQuery, WorkflowQuery, NotificationQuery, TaskRunQuery,
))
Mutation = merge_types("Mutation", (
    MemoryMutation, ConversationMutation, ArtifactMutation,
    AutomationMutation, WorkflowMutation, NotificationMutation, TaskRunMutation,
))
Subscription = merge_types("Subscription", (
    ChatSubscription, AutomationSubscription, WorkflowSubscription,
))

schema = strawberry.Schema(query=Query, mutation=Mutation, subscription=Subscription)
