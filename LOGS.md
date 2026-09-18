# 日志消息清单

> 生成自当前 `feat/mute-shell-console` 代码，共 **172** 条运行时 `log.*` 调用。  
> 控制台 `_print` 命令回执未计入。  
> 修改时按编号批注即可。

## main.py（入口/信号）

| 编号 | 级别 | 文案 |
|------|------|------|
| 001 | debug | 注册信号 %s 失败: %s |
| 002 | info | 正在停止主任务... |
| 003 | error | 主任务退出异常: %s |

## src/ai.py

| 编号 | 级别 | 文案 |
|------|------|------|
| 004 | warning | AI建议: LLM 接口返回 %s: %s |
| 005 | warning | AI建议: LLM 响应格式异常: %s |
| 006 | debug | AI建议: 客户 %d 窗口内无可读对话，跳过 |
| 007 | warning | AI建议: 为客户 %d 生成建议超时（%s 秒），跳过 |
| 008 | warning | AI建议: 为客户 %d 生成建议失败: %s |
| 009 | warning | AI建议: 客户 %d 生成异常: %s |
| 010 | info | AI建议已生成: %d/%d 名客户 |

## src/config.py

| 编号 | 级别 | 文案 |
|------|------|------|
| 011 | error | 配置重载失败，继续沿用旧配置: %s （mtime_watch） |
| 012 | warning | 配置已重载(mtime)，以下项需重启后生效: %s |
| 013 | info | 配置已重载并生效 (source=mtime_watch) |
| 014 | error | 配置重载失败，继续沿用旧配置: %s （force_reload） |
| 015 | warning | 配置已重载(source=%s)，以下项需重启后生效: %s |
| 016 | info | 配置已重载并生效 (source=%s) |

## src/group_msg.py

| 编号 | 级别 | 文案 |
|------|------|------|
| 017 | error | 获取用户 %d 昵称失败: %s |
| 018 | error | 查询消息详情失败: reply_id=%s, err=%s |
| 019 | error | 发送结束语失败: customer=%s, err=%s |
| 020 | error | 关闭会话失败: customer=%s, err=%s |
| 021 | error | 获取历史消息失败: customer=%s, err=%s |
| 022 | info | 撤回请求已超时: feedback_msg=%d, 耗时 %.1f 秒, 点击者=%d |
| 023 | error | 撤回私聊消息失败: customer=%s, msg_id=%s, err=%s |
| 024 | info | 已撤回 .say 发送的私聊消息: customer=%d, msg_id=%d, 点击者=%d |
| 025 | error | 记录撤回事件失败: customer=%s, err=%s |
| 026 | info | 内部群戳一戳触发状态面板: group=%d |
| 027 | debug | 群命令: reply_id=%s, cmd=%s |
| 028 | error | 发送私聊消息失败: customer=%s, err=%s （两段式 .say） |
| 029 | info | .list 命令执行成功，返回 %d 名客户 |
| 030 | error | 发送 .list 结果失败: %s |
| 031 | info | %s: user=%s group=%s arg=%s （reload 审计） |
| 032 | error | 发送私聊消息失败: customer=%s, err=%s （.say 带内容） |

## src/history.py

| 编号 | 级别 | 文案 |
|------|------|------|
| 033 | error | 事件兜底落盘失败: %s |
| 034 | debug | 事件因 message_id 重复被跳过: session=%s type=%s mid=%s |
| 035 | error | 事件写入失败(已落盘兜底): session=%s type=%s err=%s |
| 036 | error | 会话周期建立失败: uid=%s, err=%s |
| 037 | debug | 客户 %d 无打开的会话周期，跳过 staff_reply 记录 |
| 038 | error | 会话 %d 历史拉取失败（实时事件已在库中，跳过补录）: uid=%s, err=%s |
| 039 | error | 会话 %d 对账核对查询失败: %s |
| 040 | warning | 会话 %d 对账发现 %d 条实时已见消息未入库且未拉取到: %s |
| 041 | info | 会话 %d 历史对账完成: uid=%d, 窗口=[%.0f, %.0f], 拉取 %d 条, 补录 %d 条%s |
| 042 | error | 恢复客户 %d 会话周期失败: %s |
| 043 | error | 清理孤儿会话失败: %s |
| 044 | info | 会话周期恢复完成: 找回 %d 个, 补建 %d 个, 清理孤儿会话 %d 个 |
| 045 | error | 恢复回复耗时统计失败: %s |
| 046 | info | 已从会话库恢复 %d 条回复耗时统计 |

## src/main.py（运行时）

| 编号 | 级别 | 文案 |
|------|------|------|
| 047 | info | 收到停止请求: %s |
| 048 | info | NapCat WebSocket 连接已关闭 |
| 049 | warning | connection.close() 失败: %s |
| 050 | info | NapCat 客户端上下文已退出 |
| 051 | warning | client.__aexit__ 失败: %s |
| 052 | info | ===== 开始优雅关停（reason=%s） ===== |
| 053 | info | 正在取消 %d 个后台任务... |
| 054 | warning | 后台任务未在 5 秒内结束: %s |
| 055 | debug | 后台任务结束时异常: %s |
| 056 | warning | 关闭 NapCat 客户端时出错: %s |
| 057 | info | 运行状态已保存: %s |
| 058 | error | 关停时保存状态失败: %s |
| 059 | info | ===== 优雅关停完成 ===== |
| 060 | info | 正在连接 WebSocket... |
| 061 | info | 事件循环收到关停请求，停止处理新事件 |
| 062 | info | 启动通知已发送至群 %d |
| 063 | error | 发送启动通知失败: %s |
| 064 | info | 好友数量已初始化: %d |
| 065 | warning | 初始化好友数量失败 (尝试 %d/3): %s |
| 066 | debug | 收到事件: type=%s, post_type=%s |
| 067 | info | 好友增加: user_id=%s, 好友数=%d |
| 068 | warning | 连接断开或出错，5秒后重连... |
| 069 | info | 事件循环已取消（关停） |
| 070 | error | 事件循环异常: %s |
| 071 | info | 程序启动, WS_URL=%s, 通知群=%d, 白名单=%s |
| 072 | info | 里程碑阈值(分钟): %s |
| 073 | error | 会话库初始化/恢复失败（写库失败时事件仍会落盘兜底）: %s |

## src/message_sender.py

| 编号 | 级别 | 文案 |
|------|------|------|
| 074 | debug | 客户 %d 历史消息: 获取到 %d 条，过滤后 %d 条（%d 秒内） |
| 075 | error | 获取客户 %d 历史消息失败: %s |
| 076 | debug | 监听合并转发已更新: message_id=%s, 当前监听数=%d |
| 077 | debug | send_nested_forward: customer_list 为空，跳过发送 |
| 078 | info | 开始构造合并转发 -> 群 %d, 共 %d 名客户 |
| 079 | warning | AI建议生成失败，本次提醒不含建议: %s |
| 080 | warning | 客户 %d 无%d秒内消息，跳过该客户节点 |
| 081 | warning | 所有客户均无%d秒内消息，取消发送合并转发 |
| 082 | info | 合并转发发送成功 -> 群 %d, message_id=%s |
| 083 | error | 发送合并转发失败: %s （send_nested_forward） |
| 084 | error | 发送合并转发失败: %s （send_forward_from_message_ids） |
| 085 | debug | 已为消息 %s 添加表情 %s |
| 086 | error | 添加表情失败: message_id=%s, emoji_id=%s, err=%s |
| 087 | info | 已发送 @%d 提醒: %s |
| 088 | error | 发送 @ 提醒失败: %s |
| 089 | debug | 当前无可用成员，直接发送合并转发 |
| 090 | error | 记录客户 %d 的通知事件失败: %s |
| 091 | error | 发送反馈消息失败: %s |
| 092 | info | 会话结束: user_id=%s, 耗时=%.1f秒, 原因=%s |
| 093 | info | 自动发送结束语成功: user_id=%s |
| 094 | error | 自动发送结束语失败: user_id=%s, err=%s |
| 095 | error | 关闭会话周期失败: session=%s, err=%s |
| 096 | debug | 状态面板已发送至群 %d |
| 097 | error | 发送状态面板失败: %s |

## src/monitor.py

| 编号 | 级别 | 文案 |
|------|------|------|
| 098 | info | 巡检任务已启动，每 60 秒执行一次 |
| 099 | error | 巡检配置热重载异常: %s |
| 100 | error | 静音到期处理失败: %s |
| 101 | debug | 已清理 %d 条过期监听消息 |
| 102 | debug | 已清理 %d 条过期可撤回记录 |
| 103 | info | 已清理 %d 个超出保留期(%d天)的会话周期 |
| 104 | error | 会话周期过期清理失败: %s |
| 105 | error | 里程碑通知缺少 milestone 参数，跳过 |
| 106 | info | 夜间汇总已发送：%s |
| 107 | info | 夜间汇总后刷新好友人数: %d |
| 108 | warning | 夜间汇总后刷新好友人数失败: %s |

## src/mute.py

| 编号 | 级别 | 文案 |
|------|------|------|
| 109 | info | 已开启不限时静音（直到手动解除） |
| 110 | info | 已开启临时静音: %.1f 分钟，截止 %s |
| 111 | info | 已解除临时静音 |
| 112 | info | 临时静音已到期，自动解除 |
| 113 | info | %s：%s 通知已延后，涉及 %d 名客户 |
| 114 | info | 当前仍在夜间模式，延后通知保留至次日汇总 (reason=%s) |
| 115 | warning | 客户端未运行，延后通知暂不发送 (reason=%s) |
| 116 | info | 静音延后通知已汇总发送 (reason=%s)：%s |
| 117 | error | 静音延后通知发送失败: %s |

## src/new_user.py

| 编号 | 级别 | 文案 |
|------|------|------|
| 118 | info | 收到好友申请：uid + comment |
| 119 | info | 好友申请重复事件已忽略: flag=%s, user_id=%s |
| 120 | warning | 欢迎消息发送失败，重试 %d/%d: user_id=%s, retcode=%s, friend_count=%s |
| 121 | error | 欢迎消息全部重试失败: user_id=%s, retcode=%s, friend_count=%s, err=%s |
| 122 | error | 欢迎消息发送失败: user_id=%s, err=%s |
| 123 | error | 好友数量已达上限: friend_count=%s, limit=%s, user_id=%s |
| 124 | error | 好友申请群通知发送失败: user_id=%s, err=%s |
| 125 | info | 已自动通过好友申请: user_id=%s, comment=%s |
| 126 | error | 自动通过好友申请失败: user_id=%s, err=%s |

## src/notice_pr.py

| 编号 | 级别 | 文案 |
|------|------|------|
| 127 | info | 已清理未完成 PR 的分支 %s |
| 128 | warning | 清理分支 %s 失败: HTTP %s %s |
| 129 | warning | 清理分支 %s 异常: %s |
| 130 | error | 签发 GitHub App JWT 失败: %s |
| 131 | error | 获取 installation token 失败: HTTP %s %s |
| 132 | error | 获取 installation token 异常: %s |
| 133 | info | [notice_pr] dry_run：将提交 %s（分支 %s）\n%s |
| 134 | error | notice_pr.github.repo 未配置，无法开 PR |
| 135 | error | 无法获取 GitHub 凭据：请配置 github.token，或 App 三件套 app_id/installation_id/private_key |
| 136 | error | 获取基准分支 %s 失败: HTTP %s %s |
| 137 | info | 通知内容已存在待审 PR（分支 %s），跳过 |
| 138 | error | 创建分支 %s 失败: HTTP %s %s |
| 139 | error | 通知分支创建失败：候选分支均已存在（%s…） |
| 140 | error | 通知 PR 创建失败: group=%d user=%d msg=%s: %s |
| 141 | info | 通知 PR 已创建: #%s %s |
| 142 | error | 读取通知 ack 队列失败，按空队列处理: %s |
| 143 | error | 保存通知 ack 队列失败: %s |
| 144 | debug | 通知 PR ack 已发送至内部群 |
| 145 | error | 发送通知 PR ack 到内部群失败，加入重试队列: %s |
| 146 | info | 发现 %d 条待发送的通知 PR ack，尝试重发 |
| 147 | info | 重发通知 PR ack 成功 |
| 148 | warning | 启动重发通知 PR ack 失败，转入后台重试: %s |
| 149 | info | 重发通知 PR ack 成功 （后台重试） |
| 150 | error | 通知 PR ack 重发 %d 次仍失败，放弃: %s (%s) |
| 151 | warning | 通知 ack 后台重试轮次用尽，剩余 %d 条留待下次启动重发 |
| 152 | error | notice_pr.groups / auto_pr_senders 配置格式错误，需为 QQ 号列表 |
| 153 | info | 通知群消息为空或含非文本段，跳过自动开 PR: group=%d, user=%d, msg=%s |

## src/private_msg.py

| 编号 | 级别 | 文案 |
|------|------|------|
| 154 | info | 忽略刚通过好友申请的用户 %d 的消息（窗口期 %d 秒），不加入队列 |
| 155 | info | 新增客户 %d 进入待回复队列 (msg_id=%s, 内容: %s)。当前队列长度: %d |
| 156 | info | 客户 %d 追加消息 (msg_id=%s)，累计 %d 条，重置通报倒计时。 |
| 157 | info | 客服已回复 %s，移除提醒并记录耗时。剩余未回复: %d |
| 158 | info | 私聊戳一戳结束会话: user_id=%s |
| 159 | info | 私聊戳一戳自动发送结束语（客户不在队列）: user_id=%s |

## src/shell_console.py

| 编号 | 级别 | 文案 |
|------|------|------|
| 160 | info | Shell 控制台未启用：无可用 stdin |
| 161 | info | Shell 控制台未启用：stdin 不可用 |
| 162 | info | Shell 控制台已启动（与 bot 运行绑定），Ctrl+C 或 quit 优雅停止 |
| 163 | warning | Shell 控制台读取失败，退出: %s |
| 164 | info | Shell 控制台 stdin 关闭，控制台结束（bot 继续运行） |
| 165 | error | 控制台命令执行失败: %s |

## src/state.py

| 编号 | 级别 | 文案 |
|------|------|------|
| 166 | debug | 状态已保存至 %s |
| 167 | error | 状态保存失败: %s |
| 168 | info | 未找到状态文件 %s，将使用全新状态启动 |
| 169 | error | 状态文件读取失败: %s |
| 170 | info | 状态恢复完成：待回复客户 %d 人，监听转发 %d 条，%s |

## src/storage.py / src/utils.py

| 编号 | 级别 | 文案 |
|------|------|------|
| 171 | info | 会话库已就绪: %s |
| 172 | error | 解析时间段失败: qq=%s, slot=%s-%s, err=%s |

---
