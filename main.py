import time
import asyncio
import logging
from typing import TypedDict, Any

from napcat import NapCatClient, PrivateMessageEvent

# ================= 日志配置 =================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("qq-redot")

WS_URL = "wss://lion-qq.laysath.cn/qq-ws"
WS_TOKEN = "TYnR3DXCeM9H~O.W"

INTERNAL_GROUP_ID = 1056221119 # 内部客服通知群号
WHITELIST = [123456789, 987654321] # 免监控的白名单 QQ
MILESTONES = [5, 15, 30, 60, 120, 180, 360, 720, 1440, 2880, 4320] # 迟滞里程碑(分钟)

class CustomerData(TypedDict):
    last_active: float
    msg_ids: list[int]
    is_newly_reported: bool

# 内存字典：存储未回复的客户状态
unreplied_customers: dict[int, CustomerData] = {}

# 客户端对象
client: NapCatClient = NapCatClient(WS_URL, WS_TOKEN)

# ================= 辅助函数：构造嵌套合并转发 =================
async def send_nested_forward(group_id: int, customer_list: list[tuple[int, CustomerData]], summary_text: str):
    """
    构造嵌套合并转发并发送
    customer_list: list[tuple[int, CustomerData]]
    """
    if not customer_list:
        log.debug("send_nested_forward: customer_list 为空，跳过发送")
        return

    log.info("开始构造合并转发 -> 群 %d, 共 %d 名客户", group_id, len(customer_list))
    outer_nodes: list[dict[str, Any]] = []
    
    # 1. 头部摘要节点
    outer_nodes.append({
        "type": "node",
        "data": {
            "nickname": "客服系统提示",
            "user_id": str(client.self_id),
            "content": [{
                "type": "text",
                "data": {"text": summary_text}
            }]
        }
    })

    # 2. 为每个客户构造一个“子合并转发”节点
    for qq, data in customer_list:
        # 内层节点：该客户的所有消息 ID (使用 id 引用不需要 content)
        inner_nodes: list[dict[str, str | dict[str, str]]] = [
            {"type": "node", "data": {"id": str(msg_id)}} 
            for msg_id in data["msg_ids"]
        ]
        
        outer_nodes.append({
            "type": "node",
            "data": {
                "nickname": f"客户",
                "user_id": str(qq),
                "content": inner_nodes
            }
        })

    # 调用 SDK 混入的 send_group_forward_msg
    log.debug("合并转发节点数: %d (1 摘要 + %d 客户)", len(outer_nodes), len(customer_list))
    try:
        await client.send_group_forward_msg(
            group_id=str(group_id),
            messages=outer_nodes  # type: ignore[arg-type]
        )
        log.info("合并转发发送成功 -> 群 %d", group_id)
    except Exception as e:
        log.error("发送合并转发失败: %s", e, exc_info=True)

# ================= 核心逻辑：定时巡检任务 =================
async def monitor_loop():
    """每分钟执行一次的巡检死循环"""
    log.info("巡检任务已启动，每 60 秒执行一次")
    while True:
        await asyncio.sleep(60)
        
        if not client.is_running:
            log.debug("巡检跳过: 客户端未运行")
            continue
        if not unreplied_customers:
            log.debug("巡检跳过: 当前无未回复客户")
            continue

        log.info("===== 巡检开始 ===== 当前未回复客户数: %d", len(unreplied_customers))
        now = time.time()
        new_customers_to_report: list[tuple[int, CustomerData]] = []

        # ================= 阶段 1：新客户防抖通报 =================
        # 筛选防抖时间 >= 1分钟且未报过的新客户
        for qq, data in unreplied_customers.items():
            elapsed_min = (now - data["last_active"]) / 60.0
            if not data["is_newly_reported"] and elapsed_min >= 1.0:
                new_customers_to_report.append((qq, data))

        if new_customers_to_report:
            log.info("阶段1: 发现 %d 名新客户待通报: %s",
                     len(new_customers_to_report),
                     [qq for qq, _ in new_customers_to_report])
            # 标记已通报
            for qq, data in new_customers_to_report:
                data["is_newly_reported"] = True
            
            # 按时间更近的优先排序 (last_active 越大越靠前)
            new_customers_to_report.sort(key=lambda x: x[1]["last_active"], reverse=True)
            
            # 建议只通报新触发的客户，避免每次都全量刷屏
            summary = f"📢 刚刚有 {len(new_customers_to_report)} 名客户发来消息，请及时回复！"
            await send_nested_forward(INTERNAL_GROUP_ID, new_customers_to_report, summary)
        else:
            log.debug("阶段1: 无新客户需要通报")
            
        # ================= 阶段 2：迟滞里程碑通报 =================
        
        # 初始化按里程碑分组的字典
        milestone_groups: dict[int, list[tuple[int, CustomerData]]] = {m: [] for m in MILESTONES}
        
        for qq, data in unreplied_customers.items():
            elapsed_min = (now - data["last_active"]) / 60.0
            log.debug("  客户 %d: 已等待 %.1f 分钟, 消息数 %d", qq, elapsed_min, len(data["msg_ids"]))
            
            for m in MILESTONES:
                if abs(elapsed_min - m) <= 0.5:
                    log.debug("    -> 命中里程碑 %d 分钟", m)
                    milestone_groups[m].append((qq, data))
                    break # 单个客户在一个循环内最多只命中一个里程碑
        
        # 检查各列表，如果不为空则单独通报
        for m, delay_list in milestone_groups.items():
            if delay_list:
                # 排序：时间更近的优先
                delay_list.sort(key=lambda x: x[1]["last_active"], reverse=True)
                
                unit = f"{m}分钟" if m < 60 else f"{m//60}小时"
                log.warning("阶段2: 里程碑 %s 触发, 涉及 %d 名客户: %s",
                            unit, len(delay_list), [qq for qq, _ in delay_list])
                summary = f"⚠️ 以下 {len(delay_list)} 名客户已等待长达 {unit}！"
                await send_nested_forward(INTERNAL_GROUP_ID, delay_list, summary)

        log.info("===== 巡检结束 =====")

async def main():
    log.info("程序启动, WS_URL=%s, 通知群=%d, 白名单=%s", WS_URL, INTERNAL_GROUP_ID, WHITELIST)
    log.info("里程碑阈值(分钟): %s", MILESTONES)

    # 启动定时巡检后台任务
    asyncio.create_task(monitor_loop())

    # 自动重连
    while True:
        log.info("正在连接 WebSocket...")
        async for event in client:
            log.debug("收到事件: type=%s, post_type=%s", type(event).__name__, getattr(event, 'post_type', '?'))
            match event:
                # 1. 侦听客服的回复（清理字典）
                case PrivateMessageEvent(post_type="message_sent", target_id=tid) if tid in unreplied_customers:
                    del unreplied_customers[tid]
                    log.info("客服已回复 %s，移除提醒。剩余未回复: %d", tid, len(unreplied_customers))
                # 2. 接收客户消息 (加入/更新字典)
                case PrivateMessageEvent(post_type="message", user_id=uid, message_id=msg_id) if int(uid) not in WHITELIST:
                    uid = int(uid)
                    now = time.time()
                    if uid not in unreplied_customers:
                        unreplied_customers[uid] = {
                            "last_active": now,
                            "msg_ids": [msg_id],
                            "is_newly_reported": False
                        }
                        log.info("新增客户 %d 进入待回复队列 (msg_id=%s)。当前队列长度: %d",
                                 uid, msg_id, len(unreplied_customers))
                    else:
                        unreplied_customers[uid]["last_active"] = now
                        unreplied_customers[uid]["msg_ids"].append(msg_id)
                        unreplied_customers[uid]["is_newly_reported"] = False
                        log.info("客户 %d 追加消息 (msg_id=%s)，累计 %d 条，重置通报倒计时。",
                                 uid, msg_id, len(unreplied_customers[uid]["msg_ids"]))
                case _:
                    continue
        log.warning("连接断开或出错，5秒后重连...")
        await asyncio.sleep(5)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("程序已手动停止。")