"""耗材管理器 常量平台。"""

import logging
from typing import Final

from homeassistant.const import Platform

# ---- 基础 ----
DOMAIN: Final = "consumable_manager"
LOGGER = logging.getLogger(__name__)
PLATFORMS: Final = [Platform.SENSOR, Platform.TODO]

# ---- 条目类型 ----
CONF_ENTRY_TYPE: Final = "entry_type"
ENTRY_TYPE_STOCK: Final = "stock"
ENTRY_TYPE_CUSTOM: Final = "custom_type"
ENTRY_TYPE_NOTIFICATION: Final = "notification"

# ---- 自定义类型向导（添加界面二级界面）表单字段 ----
CONF_TYPE_KEY: Final = "type_key"            # 类型键（id），需匹配 ID_PATTERN
CONF_TYPE_NAME_ZH: Final = "type_name_zh"    # 名称（必填，单语纯字符串）
CONF_TYPE_ICON: Final = "type_icon"          # 图标（默认 mdi:package-variant）
CONF_TYPE_THRESHOLD_TYPE: Final = "type_threshold_type"
CONF_TYPE_THRESHOLD: Final = "type_threshold"
CONF_TYPE_THRESHOLD_UNIT: Final = "type_threshold_unit"

# ---- 耗材类型条目：绑定分组（多分组 → 多诊断实体）----
CONF_SOURCE_ENTITIES: Final = "source_entities"
CONF_ENTITY_REGEX: Final = "entity_regex"  # 正则输入（匹配 entity_id，一次性批量选择）
CONF_BINDING_GROUPS: Final = "binding_groups"
CONF_GROUP_ID: Final = "id"          # 分组稳定 id（实体 unique_id 用，重命名不漂移）
CONF_GROUP_NAME: Final = "name"      # 用户自定义分组名（name 标签）
CONF_SELECTED_GROUP: Final = "selected_group"  # 修改分组时选中的分组 id
CONF_REMOVE_GROUPS: Final = "remove_groups"    # 删除分组（多选，value=group_id 列表）

# 自定义耗材实体分组（不绑定实体，按 added_at 计时，直接生成诊断实体）
CONF_GROUP_KIND: Final = "kind"        # 分组类别：binding（绑定实体）/ custom（自定义耗材实体）
CONF_ADDED_AT: Final = "added_at"      # 添加/更换时间（ISO 日期，计时起点）
GROUP_KIND_BINDING: Final = "binding"  # 绑定实体分组（默认）
GROUP_KIND_CUSTOM: Final = "custom_consumable_entity"    # 自定义耗材实体分组（自建数据 → 一个诊断实体）

# ---- 耗材类型条目：阈值配置 ----
CONF_THRESHOLD_TYPE: Final = "threshold_type"
CONF_THRESHOLD: Final = "threshold"
CONF_THRESHOLD_UNIT: Final = "threshold_unit"  # 阈值单位（% / 分钟 / 小时 / 天）
CONF_THRESHOLD_OPERATOR: Final = "threshold_operator"  # 计算方式（大于 / 小于 / 等于）
CONF_MODEL: Final = "model"
CONF_LAST_REPLACED: Final = "last_replaced"  # 上次更换时间（持久化，重启不丢）
CONF_LAST_TRIGGERED_SIG: Final = "last_triggered_sig"  # 上次触发集合签名（len + 排序成员拼接，TriggeredSet 的唯一持久化基线）

# 阈值类型：由绑定实体的语义决定
THRESHOLD_TYPE_LIFETIME_PERCENT: Final = "lifetime_percent"  # 剩余寿命（%）
THRESHOLD_TYPE_NUMERIC: Final = "numeric"  # 数值（实体 state 本身即数值，无单位换算）
THRESHOLD_TYPE_REMAINING_TIME: Final = "remaining_time"  # 剩余时间（时间单位）
THRESHOLD_TYPE_USED_TIME: Final = "used_time"  # 已使用时长（时间单位）

THRESHOLD_TYPES: Final[tuple[str, ...]] = (
    THRESHOLD_TYPE_LIFETIME_PERCENT,
    THRESHOLD_TYPE_NUMERIC,
    THRESHOLD_TYPE_REMAINING_TIME,
    THRESHOLD_TYPE_USED_TIME,
)

# 阈值单位
UNIT_PERCENT: Final = "%"
UNIT_NUMERIC: Final = "numeric"  # 数值（与 type=numeric 搭配，无换算）
UNIT_MINUTES: Final = "minutes"
UNIT_HOURS: Final = "hours"
UNIT_DAYS: Final = "days"

# 时间单位 → 小时 换算系数（内部标准单位为小时）
TIME_UNIT_TO_HOURS: Final[dict[str, float]] = {
    UNIT_MINUTES: 1 / 60,
    UNIT_HOURS: 1.0,
    UNIT_DAYS: 24.0,
}

# 实体 unit_of_measurement 字符串 → 小时 换算系数（解析实体 UOM）
TIME_UOM_TO_HOURS: Final[dict[str, float]] = {
    # 小时
    "h": 1.0, "hr": 1.0, "hrs": 1.0, "hour": 1.0, "hours": 1.0,
    "小时": 1.0, "时": 1.0,
    # 分钟
    "min": 1 / 60, "mins": 1 / 60, "minute": 1 / 60, "minutes": 1 / 60,
    "分钟": 1 / 60,
    # 秒
    "s": 1 / 3600, "sec": 1 / 3600, "second": 1 / 3600, "seconds": 1 / 3600,
    "秒": 1 / 3600,
    # 天
    "d": 24.0, "day": 24.0, "days": 24.0,
    "天": 24.0, "日": 24.0,
    # 周
    "w": 168.0, "wk": 168.0, "week": 168.0, "weeks": 168.0,
    "周": 168.0, "週": 168.0,
}

# 阈值默认兜底（自定义类型不在库中时使用）
DEFAULT_THRESHOLD_TYPE: Final = THRESHOLD_TYPE_LIFETIME_PERCENT
DEFAULT_THRESHOLD: Final[float] = 20
DEFAULT_THRESHOLD_UNIT: Final = UNIT_PERCENT

# 单位下拉固定选项（所有单位，因表单静态、切换类型后选项不变）
THRESHOLD_UNIT_OPTIONS: Final[tuple[str, ...]] = (
    UNIT_PERCENT,
    UNIT_NUMERIC,
    UNIT_MINUTES,
    UNIT_HOURS,
    UNIT_DAYS,
)

# 计算方式（触发条件）
OPERATOR_GREATER_THAN: Final = "greater_than"  # 大于
OPERATOR_LESS_THAN: Final = "less_than"  # 小于
OPERATOR_EQUAL: Final = "equal"  # 等于

OPERATORS: Final[tuple[str, ...]] = (
    OPERATOR_GREATER_THAN,
    OPERATOR_LESS_THAN,
    OPERATOR_EQUAL,
)

# 各阈值类型默认计算方式
THRESHOLD_DEFAULT_OPERATOR: Final[dict[str, str]] = {
    THRESHOLD_TYPE_LIFETIME_PERCENT: OPERATOR_LESS_THAN,
    THRESHOLD_TYPE_NUMERIC: OPERATOR_LESS_THAN,
    THRESHOLD_TYPE_REMAINING_TIME: OPERATOR_LESS_THAN,
    THRESHOLD_TYPE_USED_TIME: OPERATOR_GREATER_THAN,
}

# ---- 库存条目：配置界面（添加 / 修改 / 删除库存项）----
CONF_STOCK_ITEMS: Final = "stock_items"
# 库存项字段
CONF_ITEM_ID: Final = "item_id"
CONF_ITEM_NAME: Final = "item_name"
CONF_ITEM_TYPE: Final = "item_type"  # 关联耗材类型（必选，供更换时自动扣减）
CONF_CONSUMABLE_ID: Final = "consumable_id"  # 关联内置库/用户库耗材 id（常用耗材或自定义路线写入）
CONF_QUANTITY: Final = "quantity"  # 库存数量（可为负，负数表示欠货）
CONF_UNIT: Final = "unit"  # 自定义计量单位（个 / 片 / 毫升 …）
CONF_STOCK_THRESHOLD: Final = "stock_threshold"  # 低于此数量提醒购买

# 库存管理步骤用的临时字段
CONF_SELECTED_ITEM: Final = "selected_item"
CONF_REMOVE_ITEMS: Final = "remove_items"

# 添加库存二级界面：添加方式（常用耗材 / 自定义耗材）
CONF_ADD_METHOD: Final = "add_method"
ADD_METHOD_CONSUMABLE: Final = "consumable"
ADD_METHOD_CUSTOM_CONSUMABLE: Final = "custom_consumable"

# ---- 待办事项类型 ----
TODO_KIND_REPLACE: Final = "replace"  # 更换耗材
TODO_KIND_PURCHASE: Final = "purchase"  # 购买耗材

TODO_KINDS: Final[tuple[str, ...]] = (TODO_KIND_REPLACE, TODO_KIND_PURCHASE)

# ---- 状态机常量（库存/更换状态 + 待办状态；协调器共享单一来源）----
STATE_OK: Final = "ok"
STATE_LOW_STOCK: Final = "low_stock"  # 库存条目：有库存项低于阈值
STATE_REPLACE_NEEDED: Final = "replace_needed"  # 耗材类型条目：有绑定实体越过阈值

TODO_STATUS_NEEDS_ACTION: Final = "needs_action"
TODO_STATUS_COMPLETED: Final = "completed"

UPDATE_INTERVAL_SECONDS: Final[int] = 60

# ---- 通知（全局通知条目 + 条目级覆盖）----
# 通知条目：承载全局通知配置，不生成任何实体
CONF_NOTIFICATION: Final = "notification"  # 通知配置段
CONF_NOTIFY_SYSTEM: Final = "system"  # HA 系统通知（persistent_notification）
CONF_NOTIFY_ENTITIES: Final = "entities"  # 通知实体（notify 域，可多选）
CONF_NOTIFY_MODE: Final = "mode"  # 推送模式：实时 / 定时统一推送
CONF_NOTIFY_SCHEDULE_TIME: Final = "schedule_time"  # 定时推送时刻（HH:MM，仅定时模式）
CONF_NOTIFY_STYLE: Final = "style"  # 消息样式：人性化文案 / 状态值
CONF_NOTIFY_CUSTOMIZE: Final = "customize"  # 条目级表单开关：自定义本条目通知（不落盘）

# ---- 推送模式 ----
NOTIFY_MODE_REALTIME: Final = "realtime"  # 触发即推送
NOTIFY_MODE_SCHEDULED: Final = "scheduled"  # 每天固定时刻统一推送（合并消息）
NOTIFY_MODES: Final[tuple[str, ...]] = (
    NOTIFY_MODE_REALTIME,
    NOTIFY_MODE_SCHEDULED,
)

# ---- 消息样式 ----
NOTIFY_STYLE_HUMAN: Final = "human"  # 人性化文案：「书房温湿度传感器电量低，请更换。」
NOTIFY_STYLE_VALUE: Final = "value"  # 状态值：「书房温湿度传感器 18%」
NOTIFY_STYLES: Final[tuple[str, ...]] = (
    NOTIFY_STYLE_HUMAN,
    NOTIFY_STYLE_VALUE,
)

# 定时时刻兜底（条目级留空且全局未配时使用）
NOTIFY_DEFAULT_SCHEDULE_TIME: Final = "20:00"

# ---- 通知 / 待办固定话术（翻译键 selector.notify_text.*）----
NOTIFY_TEXT_LOW_STOCK: Final = "low_stock"  # 「库存告急，请购买。」
NOTIFY_TEXT_REPLACE_NEEDED: Final = "replace_needed"  # 「请更换耗材。」（通用话术）
NOTIFY_TEXT_LAST_REPLACED: Final = "last_replaced"  # 「上次更换：」
NOTIFY_TEXT_CONSUMABLES: Final = "consumables"  # 「耗材：」
# 更换待办描述标签
NOTIFY_TEXT_DESC_AREA: Final = "desc_area"  # 「区域」
NOTIFY_TEXT_DESC_DEVICE: Final = "desc_device"  # 「设备」
NOTIFY_TEXT_DESC_ENTITY: Final = "desc_entity"  # 「实体」
NOTIFY_TEXT_DESC_SPECS: Final = "desc_specs"  # 「规格」（耗材库 meta）
NOTIFY_TEXT_DESC_THRESHOLD: Final = "desc_threshold"  # 「阈值」（库存描述）
NOTIFY_TEXT_UNKNOWN: Final = "unknown"  # 「未知」（未绑定耗材）
NOTIFY_TEXTS: Final[tuple[str, ...]] = (
    NOTIFY_TEXT_LOW_STOCK,
    NOTIFY_TEXT_REPLACE_NEEDED,
    NOTIFY_TEXT_LAST_REPLACED,
    NOTIFY_TEXT_CONSUMABLES,
    NOTIFY_TEXT_DESC_AREA,
    NOTIFY_TEXT_DESC_DEVICE,
    NOTIFY_TEXT_DESC_ENTITY,
    NOTIFY_TEXT_DESC_SPECS,
    NOTIFY_TEXT_DESC_THRESHOLD,
    NOTIFY_TEXT_UNKNOWN,
)

# ---- 条目排序前缀（图标排在文字前 → 固定置顶；协调器展示前剥离）----
ENTRY_SORT_PREFIXES: Final[dict[str, str]] = {
    ENTRY_TYPE_STOCK: "\U0001F4E6 ",
    ENTRY_TYPE_NOTIFICATION: "\u23F0 ",
}