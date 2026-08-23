"""Schema root — composes Query, Mutation, Subscription from per-domain modules."""

from __future__ import annotations

import strawberry
from strawberry.tools import merge_types

from .extensions import SerializeSessionResolvers

from .mutations.approval import ApprovalMutation
from .mutations.artifact import ArtifactMutation
from .mutations.automation import AutomationMutation
from .mutations.board_task import BoardTaskMutation
from .mutations.conversation import ConversationMutation
from .mutations.mcp import McpMutation
from .mutations.memory import MemoryMutation
from .mutations.models import ModelsMutation
from .mutations.notification import NotificationMutation
from .mutations.project import ProjectMutation
from .mutations.skill import SkillMutation
from .mutations.task_run import TaskRunMutation
from .mutations.tool import ToolMutation
from .mutations.workflow import WorkflowMutation
from .queries.approval import ApprovalQuery
from .queries.artifact import ArtifactQuery
from .queries.automation import AutomationQuery
from .queries.board_task import BoardTaskQuery
from .queries.conversation import ConversationQuery
from .queries.mcp import McpQuery
from .queries.memory import MemoryQuery
from .queries.models import ModelsQuery
from .queries.notification import NotificationQuery
from .queries.project import ProjectQuery
from .queries.skill import SkillQuery
from .queries.task_run import TaskRunQuery
from .queries.tool import ToolQuery
from .queries.workflow import WorkflowQuery
from .subscriptions.automation import AutomationSubscription
from .subscriptions.board_task import BoardTaskSubscription
from .subscriptions.chat import ChatSubscription
from .subscriptions.workflow import WorkflowSubscription

Query = merge_types("Query", (
    ModelsQuery, MemoryQuery, ConversationQuery, ArtifactQuery,
    AutomationQuery, BoardTaskQuery, WorkflowQuery, NotificationQuery,
    ProjectQuery, SkillQuery, TaskRunQuery, McpQuery, ApprovalQuery,
    ToolQuery,
))
Mutation = merge_types("Mutation", (
    MemoryMutation, ConversationMutation, ArtifactMutation,
    AutomationMutation, BoardTaskMutation, WorkflowMutation,
    NotificationMutation, ProjectMutation, SkillMutation, TaskRunMutation,
    McpMutation, ModelsMutation, ApprovalMutation, ToolMutation,
))
Subscription = merge_types("Subscription", (
    ChatSubscription, AutomationSubscription, BoardTaskSubscription,
    WorkflowSubscription,
))

schema = strawberry.Schema(
    query=Query,
    mutation=Mutation,
    subscription=Subscription,
    # Resolvers share one AsyncSession per request; see extensions.py.
    extensions=[SerializeSessionResolvers],
)
