"""工具定义与安全层。

LLM 的输出是不可信的输入，必须和用户输入一样校验：
- 每个工具一个 Pydantic 参数模型，`extra="forbid"`，LLM 不能塞进模型没定义的字段（比如 user_id）。
- 同一份模型既用 `model_json_schema()` 导出给 LLM 看的 JSON Schema，又用来校验 LLM 的返回，
  两边不会出现"给 LLM 看的格式"和"校验用的格式"对不上的问题。
- `parse_tool_call()` 是唯一入口：不在注册表里的工具名一律拒绝（白名单），参数校验不过直接返回
  失败原因，调用方（2.7 LangGraph 编排）校验不过就不能执行任何动作。
"""
import enum
import json
from dataclasses import dataclass, field
from datetime import date as date_type
from typing import Any, Callable, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

# ---------- 各工具的参数模型 ----------


class SearchKnowledgeArgs(BaseModel):
    """知识问答检索"""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=200, description="要检索的问题原文或改写后的问题")


class QueryFinanceArgs(BaseModel):
    """财务查询"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["orders", "bills", "invoices", "refunds", "balance"] = Field(description="查询哪类财务数据")
    period: Optional[str] = Field(
        default=None, description="查询的时间范围：last_month（上个月）、this_month（本月）或 YYYY-MM"
    )
    # 可选：家长/本人指定要查的对象；不传默认查自己。真正能不能查由 can_access_finance() 决定，
    # 这里只负责格式校验——校验通过不代表有权限查
    target_user_id: Optional[str] = Field(
        default=None, description="要查询的用户 id，不传则查询发起人自己"
    )

    @field_validator("period")
    @classmethod
    def _check_period(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v in ("last_month", "this_month"):
            return v
        import re

        if re.fullmatch(r"\d{4}-\d{2}", v):
            return v
        raise ValueError("period 只能是 last_month、this_month 或 YYYY-MM")

    @field_validator("target_user_id")
    @classmethod
    def _check_target_user_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        import re

        if not re.fullmatch(r"u_[a-z]_\d{4}", v):
            raise ValueError("target_user_id 格式不对，应形如 u_a_1001")
        return v


class PlatformCommandAction(str, enum.Enum):
    open_schedule = "open_schedule"
    query_study_report = "query_study_report"
    update_course_reminder = "update_course_reminder"
    disable_auto_renew = "disable_auto_renew"
    enable_auto_renew = "enable_auto_renew"
    submit_leave = "submit_leave"


# 高风险动作清单（代码常量，见 PHASE2.md 第 2 节）：是不是高风险由代码判断，不由 LLM 判断，
# LLM 只负责说清楚用户想做什么、参数是什么
HIGH_RISK_PLATFORM_ACTIONS: frozenset[PlatformCommandAction] = frozenset(
    {
        PlatformCommandAction.disable_auto_renew,
        PlatformCommandAction.enable_auto_renew,
        PlatformCommandAction.submit_leave,
    }
)


class PlatformCommandArgs(BaseModel):
    """平台客户端指令"""

    model_config = ConfigDict(extra="forbid")

    action: PlatformCommandAction
    course_name: Optional[str] = Field(default=None, description="课程名，如“春季数学班”")
    # 字段名 date 和 datetime.date 类型同名，标注时用别名 date_type 指代类型，避免类命名空间里
    # 字段名把类型名挡住（Pydantic 解析注解时会先查类自己的命名空间）
    date: Optional[date_type] = Field(default=None, description="请假等动作涉及的日期")
    reason: Optional[str] = Field(default=None, max_length=100, description="原因说明，最长 100 字")


class ManageReminderAction(str, enum.Enum):
    create = "create"
    update = "update"
    cancel = "cancel"
    view = "view"


class ManageReminderArgs(BaseModel):
    """日程提醒：创建、修改、取消、查看四个动作共用一个工具，字段是否必填按 action 校验——
    跟 PlatformCommandArgs 三个高风险动作共用一个模型是同一个思路，对外始终只有一个
    manage_reminder 工具，不用为四个动作各注册一个函数。

    时间由 LLM 理解、代码检查（PHASE3.md 关键设计决定 6）：event_time 是 LLM 把"明天 9 点"
    这类说法转换成的本地时间字符串，这里只做结构性校验（格式对不对）；是不是真的在将来、
    时区合不合法、提前量范围对不对，交给 app.common.reminder_rules（那边要连时区库，
    不是纯粹的 JSON Schema 校验，放在这一层不合适）。

    reminder_id 不强制要求 update/cancel 必填：LLM 应该从 <提醒列表> 块里选一个 id 填进来，
    但选不出来（用户没说清楚，或者压根没有生效中的提醒）也是一种合法结果，交给
    app/worker/graph/reminder.py 按"0 条/1 条/多条"分别处理，不在这一层就直接拒绝。
    """

    model_config = ConfigDict(extra="forbid")

    action: ManageReminderAction
    title: Optional[str] = Field(default=None, max_length=100, description="提醒标题，如“交作业”“家长会”")
    # 形如 "2026-09-26 09:00"，是不是在将来、时区合不合法由 reminder_rules 校验，这里只挡格式
    event_time: Optional[str] = Field(
        default=None, description="LLM 理解后的本地时间，格式 YYYY-MM-DD HH:MM"
    )
    repeat: Optional[Literal["none", "daily", "weekly", "workdays"]] = Field(
        default=None, description="重复规则，不传视为 none（只提醒一次）"
    )
    advance_minutes: Optional[int] = Field(default=None, ge=0, le=1440, description="提前多少分钟提醒")
    # 修改/取消时从 <提醒列表> 块里选一个 id；代码会再查一遍这个 id 是不是真的属于当前
    # tenant/user，不属于就按越权处理，不能信任 LLM 输出的 id 一定合法
    reminder_id: Optional[str] = Field(default=None, description="要修改/取消的提醒 id")

    @field_validator("event_time")
    @classmethod
    def _check_event_time_format(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        import re

        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}", v.strip()):
            raise ValueError("event_time 格式必须是 YYYY-MM-DD HH:MM")
        return v

    @model_validator(mode="after")
    def _check_required_by_action(self) -> "ManageReminderArgs":
        # 只在这里挡"创建提醒但连标题/时间都没有"这种明显残缺的输出；update/cancel/view
        # 需不需要 reminder_id、有没有传其它字段，是业务节点自己按实际查到的数据判断的事
        if self.action == ManageReminderAction.create and (not self.title or not self.event_time):
            raise ValueError("创建提醒必须提供 title 和 event_time")
        return self


class TransferToHumanArgs(BaseModel):
    """转人工"""

    model_config = ConfigDict(extra="forbid")

    reason: Optional[str] = Field(default=None, description="转人工的原因，可选")


# ---------- 工具注册表 ----------


@dataclass(frozen=True)
class ToolSpec:
    model: type[BaseModel]
    # 判断某一次调用是不是高风险；默认恒为 False。只有 platform_command 按 action 判断
    # （见 HIGH_RISK_PLATFORM_ACTIONS），其余工具本身就不在高风险清单里
    is_high_risk: Callable[[BaseModel], bool] = field(default=lambda args: False)
    # 真正执行这个工具的处理函数，阶段二 2.8~2.11 陆续接入（knowledge/finance/command/handoff 节点），
    # 这一步只搭好注册表的形状，不提前实现还没到的业务节点
    handler: Optional[Callable[..., Any]] = None


def _platform_command_is_high_risk(args: BaseModel) -> bool:
    assert isinstance(args, PlatformCommandArgs)
    return args.action in HIGH_RISK_PLATFORM_ACTIONS


TOOL_REGISTRY: dict[str, ToolSpec] = {
    "search_knowledge": ToolSpec(model=SearchKnowledgeArgs),
    "query_finance": ToolSpec(model=QueryFinanceArgs),
    "platform_command": ToolSpec(model=PlatformCommandArgs, is_high_risk=_platform_command_is_high_risk),
    "manage_reminder": ToolSpec(model=ManageReminderArgs),
    "transfer_to_human": ToolSpec(model=TransferToHumanArgs),
}


def to_openai_tools() -> list[dict[str, Any]]:
    """从注册表的 Pydantic 模型直接导出 OpenAI function-calling 格式的工具列表。

    和 parse_tool_call() 用的是同一份模型，给 LLM 看的格式和校验 LLM 返回值的格式不可能对不上。
    """
    tools = []
    for name, spec in TOOL_REGISTRY.items():
        schema = spec.model.model_json_schema()
        # Pydantic v2 生成的 schema 会带 "title"，OpenAI 的 function.parameters 不需要，去掉更干净
        schema.pop("title", None)
        description = (spec.model.__doc__ or name).strip()
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": schema,
                },
            }
        )
    return tools


@dataclass(frozen=True)
class ParsedToolCall:
    name: str
    args: BaseModel
    is_high_risk: bool


@dataclass(frozen=True)
class ToolCallError:
    reason: Literal["invalid_json", "unknown_tool", "schema_error"]
    detail: str
    # 只有 schema_error 会填：校验失败涉及的字段名（如 target_user_id、kind）
    fields: tuple[str, ...] = ()


def parse_tool_call(name: str, arguments_json: str) -> Union[ParsedToolCall, ToolCallError]:
    """LLM 输出不能直接执行：这是唯一的校验入口，调用方只在拿到 ParsedToolCall 时才能往下执行。"""
    spec = TOOL_REGISTRY.get(name)
    if spec is None:
        # 白名单：不在注册表里的工具名一律拒绝，不管参数长什么样
        return ToolCallError(reason="unknown_tool", detail=f"未知工具：{name}")

    try:
        raw_args = json.loads(arguments_json)
    except json.JSONDecodeError as exc:
        return ToolCallError(reason="invalid_json", detail=str(exc))

    try:
        args = spec.model.model_validate(raw_args)
    except ValidationError as exc:
        fields = tuple(".".join(str(p) for p in err["loc"]) for err in exc.errors())
        return ToolCallError(reason="schema_error", detail=str(exc), fields=fields)

    return ParsedToolCall(name=name, args=args, is_high_risk=spec.is_high_risk(args))
