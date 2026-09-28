"""
Telegram 消息监听器 — 使用 Telethon 实时监控带单群消息。
"""
import asyncio
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from telethon import TelegramClient, events
from telethon.errors import (
    AuthKeyError, AuthKeyNotFound, AuthKeyUnregisteredError, AuthKeyDuplicatedError,
    FloodWaitError, PhoneNumberInvalidError,
    ApiIdInvalidError, ApiIdPublishedFloodError,
)
from loguru import logger

try:
    from core.states import ServiceRegistry, ModuleState, MOD_TELEGRAM
    _STATES_AVAILABLE = True
except Exception:
    _STATES_AVAILABLE = False
    ServiceRegistry = None

# 本地开发（Windows）session 放项目根目录，服务器（Linux）放 user_data/sessions
if sys.platform.startswith("win"):
    SESSION_DIR = Path(".")
else:
    SESSION_DIR = Path("user_data/sessions")


def _telethon_proxy() -> tuple | None:
    raw = os.getenv("EXCHANGE_PROXY", "").strip()
    if not raw:
        return None
    u = urlparse(raw)
    if not u.hostname or not u.port:
        return None
    scheme = (u.scheme or "http").lower()
    proxy_type = "socks5" if scheme.startswith("socks") else "http"
    return (proxy_type, u.hostname, u.port)


def _set_state(state):
    if _STATES_AVAILABLE:
        ServiceRegistry.set_state(MOD_TELEGRAM, state)


def _default_session_name() -> str:
    """根据环境自动选择 session 名称，隔离本地和服务器 session

    优先级：
    1. 环境变量 SESSION_NAME（从 .env 读取）
    2. 操作系统自动判断（Windows -> telegram_dev, Linux/Mac -> telegram_server）

    示例：
    - Windows 默认使用: telegram_dev.session（项目根目录）
    - Linux 默认使用: user_data/sessions/telegram_server.session
    - 可在 .env 中设置 SESSION_NAME=xxx 覆盖
    """
    # 优先从环境变量读取
    session_name = os.getenv("SESSION_NAME", "").strip()
    if session_name:
        return session_name

    # 根据操作系统自动判断，避免本地/服务器 session 冲突
    if sys.platform.startswith("win"):
        return "telegram_dev"
    else:
        return "telegram_server"


class TelegramListener:

    def __init__(
        self,
        api_id: int,
        api_hash: str,
        target_group_titles: list[str],
        whitelist_senders: list[str] | None = None,
        session_name: str | None = None,
    ):
        self.api_id = api_id
        self.api_hash = api_hash
        self.target_titles = target_group_titles
        self.whitelist = set(whitelist_senders or [])
        self.session_name = session_name or _default_session_name()
        self.client: TelegramClient | None = None
        self.message_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._queue_full_warned = False
        self._started = False

    def _title_match(self, title: str) -> bool:
        for t in self.target_titles:
            if t in title or title in t:
                return True
        return False

    def _is_trading_signal(self, text: str) -> bool:
        """检测消息是否包含交易信号特征"""
        if not text:
            return False

        # 转换为小写以便匹配
        text_lower = text.lower()

        # 币种特征
        currency_patterns = [
            r'#btc', r'#eth', r'#sol', r'#doge', r'#xrp',
            r'btcusdt', r'ethusdt', r'solusdt', r'dogeusdt', r'xrpusdt',
            r'\bbtc\b', r'\beth\b', r'\bsol\b', r'\bdoge\b', r'\bxrp\b',
        ]

        # 方向特征
        direction_patterns = [
            r'\blong\b', r'\bshort\b',
            r'\bbuy\b', r'\bsell\b',
            r'\b开仓\b', r'\b入场\b', r'\b做多\b', r'\b做空\b',
        ]

        # 价格/订单特征
        price_patterns = [
            r'\bentry\b', r'\bentries\b',
            r'\blimit\b', r'\bmarket\b',
            r'\bopen\s*position\b',
            r'\b开仓\b', r'\b入场\b',
        ]

        # 检查是否包含任一特征
        for pattern in currency_patterns + direction_patterns + price_patterns:
            if re.search(pattern, text_lower):
                return True

        return False

    async def start(self) -> bool:
        if self._started:
            logger.warning("Telegram 客户端已启动，禁止重复 start()")
            return True

        _set_state(ModuleState.INITIALIZING)
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        session_path = str(SESSION_DIR / self.session_name)
        session_file = f"{session_path}.session"
        if os.path.exists(session_file):
            try:
                with open(session_file, "r+b") as _sf:
                    try:
                        _sf.flush()
                    except OSError:
                        logger.warning(f"Session 文件被锁定，重新创建: {session_file}")
                        os.remove(session_file)
            except (PermissionError, OSError):
                logger.warning(f"Session 文件无法访问，重新创建: {session_file}")
                try:
                    os.remove(session_file)
                except Exception:
                    pass
            except Exception:
                pass

        proxy = _telethon_proxy()
        if proxy:
            logger.info(f"Telegram 代理: {proxy[0]}://{proxy[1]}:{proxy[2]}")

        self.client = TelegramClient(
            session_path, self.api_id, self.api_hash,
            proxy=proxy,
            system_version="4.16.30-vx-custom",
            device_model="TradingBot",
            app_version="1.0.0",
        )

        phone = os.getenv("TG_PHONE", "").strip() or None
        logger.info("Telegram 客户端正在连接...")
        try:
            await self.client.start(phone=phone)
        except (AuthKeyError, AuthKeyNotFound, AuthKeyUnregisteredError, AuthKeyDuplicatedError) as e:
            logger.error(f"Telegram 登录失败：Session 已损坏或失效 ({type(e).__name__})")
            await self.client.disconnect()
            try:
                os.remove(session_file)
                logger.info("  session 文件已删除，下次启动将重新登录")
            except Exception as _del_err:
                logger.warning(f"  删除 session 文件失败: {_del_err}，请手动删除")
            _set_state(ModuleState.FAILED)
            return False
        except (ApiIdInvalidError, ApiIdPublishedFloodError) as e:
            logger.error(f"Telegram 登录失败：API ID 无效 ({type(e).__name__})")
            logger.error("  请检查 .env 中的 TG_API_ID 和 TG_API_HASH")
            await self.client.disconnect()
            _set_state(ModuleState.FAILED)
            return False
        except (PhoneNumberInvalidError,) as e:
            logger.error(f"Telegram 登录失败：手机号无效 ({type(e).__name__})")
            await self.client.disconnect()
            _set_state(ModuleState.FAILED)
            return False
        except FloodWaitError as e:
            logger.error(f"Telegram 登录失败：被限流，需等待 {e.seconds} 秒")
            await self.client.disconnect()
            _set_state(ModuleState.FAILED)
            return False
        except ConnectionError as e:
            logger.error(f"Telegram 连接失败: {e}")
            await self.client.disconnect()
            _set_state(ModuleState.FAILED)
            return False
        except OSError as e:
            logger.error(f"Telegram 连接异常 (OSError): {e}")
            await self.client.disconnect()
            _set_state(ModuleState.FAILED)
            return False
        except Exception as e:
            logger.error(f"Telegram 启动失败: {type(e).__name__}: {e}")
            logger.error(f"  删除 session 文件后重试: {session_path}.session")
            await self.client.disconnect()
            _set_state(ModuleState.FAILED)
            return False

        logger.info(f"  client.start() -> OK")
        if not self.client.is_connected():
            logger.error("Telegram 连接验证失败：客户端未连接")
            await self.client.disconnect()
            _set_state(ModuleState.FAILED)
            return False
        logger.info(f"  client.is_connected() -> True")
        authorized = await self.client.is_user_authorized()
        logger.info(f"  client.is_user_authorized() -> {authorized}")
        if not authorized:
            logger.error("Telegram 登录验证失败：用户未授权")
            await self.client.disconnect()
            _set_state(ModuleState.FAILED)
            return False
        self._started = True

        try:
            me = await self.client.get_me()
            logger.success(f"Telegram 登录成功: {me.first_name} (@{me.username})")
        except Exception as e:
            logger.error(f"获取 Telegram 账号信息失败: {type(e).__name__}: {e}")
            await self.client.disconnect()
            self._started = False
            _set_state(ModuleState.FAILED)
            return False

        logger.info("正在拉取群组列表，按名称匹配...")
        resolved = []
        try:
            dialogs = await self.client.get_dialogs(limit=200)
        except Exception as e:
            logger.error(f"拉取群组列表失败: {type(e).__name__}: {e}")
            await self.client.disconnect()
            self._started = False
            _set_state(ModuleState.FAILED)
            return False

        for target in self.target_titles:
            found = None
            for d in dialogs:
                title = getattr(d, "title", "") or d.name or ""
                if target in title or title in target:
                    found = d.entity
                    logger.info(f"监听 -> [{title}]")
                    break
            if found:
                resolved.append(found)
            else:
                logger.warning(f"未找到群: {target}")

        if not resolved:
            logger.error("没有匹配到任何目标群组")
            await self.client.disconnect()
            self._started = False
            _set_state(ModuleState.FAILED)
            return False

        @self.client.on(events.NewMessage(chats=resolved))
        async def handler(event: events.NewMessage.Event):
            msg = event.message
            if not msg.text:
                return
            sender = msg.sender
            sender_name = self._name(sender)
            if self.whitelist:
                uname = getattr(sender, "username", "") or ""
                if uname not in self.whitelist and str(sender.id) not in self.whitelist and sender_name.strip() not in self.whitelist:
                    return
            chat = await event.get_chat()
            if self.message_queue.full():
                if not self._queue_full_warned:
                    logger.warning("消息队列已满(1000条)，丢弃旧消息")
                    self._queue_full_warned = True
                try:
                    self.message_queue.get_nowait()
                    self.message_queue.task_done()
                except asyncio.QueueEmpty:
                    pass
            else:
                self._queue_full_warned = False
            # ====== 回复链遍历：提取原始交易信号 ======
            reply_to_id = msg.reply_to.reply_to_msg_id if msg.reply_to else None
            reply_text = ""
            original_signal_text = ""  # 原始交易信号
            reply_chain = []  # 回复链（用于日志）

            if reply_to_id:
                logger.info(f"[Reply Context] 检测到回复消息 MsgID={msg.id}, 回复至 MsgID={reply_to_id}")

                # 递归遍历回复链，最多 10 层
                current_msg = msg
                max_depth = 10
                depth = 0

                while depth < max_depth:
                    try:
                        parent_msg = await current_msg.get_reply_message()
                        if not parent_msg or not parent_msg.text:
                            break

                        reply_chain.append({
                            "msg_id": parent_msg.id,
                            "text": parent_msg.text[:100],  # 截取前 100 字符用于日志
                        })

                        # 检测是否包含交易信号特征
                        if self._is_trading_signal(parent_msg.text):
                            original_signal_text = parent_msg.text
                            logger.info(f"[Reply Context] 找到原始交易信号 MsgID={parent_msg.id}")
                            break

                        current_msg = parent_msg
                        depth += 1

                    except Exception as e:
                        logger.warning(f"[Reply Context] 获取回复消息失败: {e}")
                        break

                # 获取直接回复的消息文本（用于构建上下文）
                try:
                    reply_msg = await msg.get_reply_message()
                    if reply_msg and reply_msg.text:
                        reply_text = reply_msg.text
                except Exception:
                    pass

                # 如果没找到原始信号，但有回复链，使用最顶层的消息
                if not original_signal_text and reply_chain:
                    top_msg = reply_chain[-1]
                    original_signal_text = top_msg.get("text", "")
                    logger.info(f"[Reply Context] 未找到明确信号，使用最顶层消息 MsgID={top_msg.get('msg_id')}")

            await self.message_queue.put({
                "tg_msg_id": msg.id,
                "tg_group_id": str(chat.id),
                "tg_group_title": getattr(chat, "title", ""),
                "tg_sender_id": str(sender.id) if sender else "0",
                "tg_sender_name": sender_name if sender else "Unknown",
                "text": msg.text,
                "date": msg.date,
                "reply_to_msg_id": reply_to_id,
                "reply_text": reply_text,
                "original_signal": original_signal_text,  # 新增：原始交易信号
                "reply_chain": reply_chain,  # 新增：回复链（用于日志）
            })

        logger.success(f"Telegram 监听已启动 ({len(resolved)} 个群组)")
        _set_state(ModuleState.CONNECTED)
        return True

    def _name(self, sender) -> str:
        if not sender:
            return "unknown"
        first = getattr(sender, "first_name", "") or ""
        last = getattr(sender, "last_name", "") or ""
        username = getattr(sender, "username", "") or ""
        if username:
            return f"{first} {last}(@{username})".strip()
        return f"{first} {last}".strip() or str(sender.id)

    @property
    def is_connected(self) -> bool:
        return self._started and self.client is not None and self.client.is_connected()

    async def reconnect(self) -> bool:
        """
        P0-2: Telegram 自动恢复（指数退避重连）。

        指数退避策略：
        - 第1次: 5s
        - 第2次: 10s
        - 第3次: 20s
        - 第4次: 40s
        - 第5次及以后: 60s

        返回：
            True: 重连成功
            False: 重连失败（但不会抛出异常）
        """
        if not self._started:
            logger.warning("[Telegram] 未启动，无法重连")
            return False

        # 指数退避延迟（秒）
        backoff_delays = [5, 10, 20, 40, 60]

        for attempt in range(1, 11):  # 最多尝试10次
            delay = backoff_delays[min(attempt - 1, len(backoff_delays) - 1)]
            logger.info(f"[Telegram] reconnect... (attempt {attempt}/10, delay={delay}s)")

            try:
                # 1. 断开旧连接
                if self.client:
                    try:
                        await self.client.disconnect()
                    except Exception:
                        pass
                    self.client = None

                # 2. 等待退避时间
                await asyncio.sleep(delay)

                # 3. 重新创建客户端
                session_path = str(SESSION_DIR / self.session_name)
                proxy = _telethon_proxy()

                self.client = TelegramClient(
                    session_path, self.api_id, self.api_hash,
                    proxy=proxy,
                    system_version="4.16.30-vx-custom",
                    device_model="TradingBot",
                    app_version="1.0.0",
                )

                # 4. 连接
                phone = os.getenv("TG_PHONE", "").strip() or None
                await self.client.start(phone=phone)

                # 5. 验证连接
                if not self.client.is_connected():
                    logger.warning(f"[Telegram] 连接验证失败 (attempt {attempt})")
                    continue

                # 6. 验证授权
                if not await self.client.is_user_authorized():
                    logger.warning(f"[Telegram] 授权验证失败 (attempt {attempt})")
                    continue

                # 7. 重新注册事件处理器
                dialogs = await self.client.get_dialogs(limit=200)
                resolved = []
                for target in self.target_titles:
                    for d in dialogs:
                        title = getattr(d, "title", "") or d.name or ""
                        if target in title or title in target:
                            resolved.append(d.entity)
                            break

                if not resolved:
                    logger.error(f"[Telegram] 未找到目标群组 (attempt {attempt})")
                    continue

                @self.client.on(events.NewMessage(chats=resolved))
                async def handler(event: events.NewMessage.Event):
                    msg = event.message
                    if not msg.text:
                        return
                    sender = msg.sender
                    sender_name = self._name(sender)
                    if self.whitelist:
                        uname = getattr(sender, "username", "") or ""
                        if uname not in self.whitelist and str(sender.id) not in self.whitelist and sender_name.strip() not in self.whitelist:
                            return
                    chat = await event.get_chat()
                    if self.message_queue.full():
                        if not self._queue_full_warned:
                            logger.warning("消息队列已满(1000条)，丢弃旧消息")
                            self._queue_full_warned = True
                        try:
                            self.message_queue.get_nowait()
                            self.message_queue.task_done()
                        except asyncio.QueueEmpty:
                            pass
                    else:
                        self._queue_full_warned = False

                    # ====== 回复链遍历：提取原始交易信号 ======
                    reply_to_id = msg.reply_to.reply_to_msg_id if msg.reply_to else None
                    reply_text = ""
                    original_signal_text = ""  # 原始交易信号
                    reply_chain = []  # 回复链（用于日志）

                    if reply_to_id:
                        logger.info(f"[Reply Context] 检测到回复消息 MsgID={msg.id}, 回复至 MsgID={reply_to_id}")

                        # 递归遍历回复链，最多 10 层
                        current_msg = msg
                        max_depth = 10
                        depth = 0

                        while depth < max_depth:
                            try:
                                parent_msg = await current_msg.get_reply_message()
                                if not parent_msg or not parent_msg.text:
                                    break

                                reply_chain.append({
                                    "msg_id": parent_msg.id,
                                    "text": parent_msg.text[:100],  # 截取前 100 字符用于日志
                                })

                                # 检测是否包含交易信号特征
                                if self._is_trading_signal(parent_msg.text):
                                    original_signal_text = parent_msg.text
                                    logger.info(f"[Reply Context] 找到原始交易信号 MsgID={parent_msg.id}")
                                    break

                                current_msg = parent_msg
                                depth += 1

                            except Exception as e:
                                logger.warning(f"[Reply Context] 获取回复消息失败: {e}")
                                break

                        # 获取直接回复的消息文本（用于构建上下文）
                        try:
                            reply_msg = await msg.get_reply_message()
                            if reply_msg and reply_msg.text:
                                reply_text = reply_msg.text
                        except Exception:
                            pass

                        # 如果没找到原始信号，但有回复链，使用最顶层的消息
                        if not original_signal_text and reply_chain:
                            top_msg = reply_chain[-1]
                            original_signal_text = top_msg.get("text", "")
                            logger.info(f"[Reply Context] 未找到明确信号，使用最顶层消息 MsgID={top_msg.get('msg_id')}")

                    await self.message_queue.put({
                        "tg_msg_id": msg.id,
                        "tg_group_id": str(chat.id),
                        "tg_group_title": getattr(chat, "title", ""),
                        "tg_sender_id": str(sender.id),
                        "tg_sender_name": sender_name,
                        "text": msg.text,
                        "date": msg.date,
                        "reply_to_msg_id": reply_to_id,
                        "reply_text": reply_text,
                        "original_signal": original_signal_text,  # 新增：原始交易信号
                        "reply_chain": reply_chain,  # 新增：回复链（用于日志）
                    })

                logger.success(f"[Telegram] reconnected. (attempt {attempt}, groups={len(resolved)})")
                _set_state(ModuleState.CONNECTED)
                return True

            except Exception as e:
                logger.warning(f"[Telegram] 重连异常 (attempt {attempt}): {type(e).__name__}: {e}")
                continue

        logger.error("[Telegram] 重连失败（已尝试10次）")
        _set_state(ModuleState.FAILED)
        return False

    async def health_check(self) -> bool:
        """
        P0-2: Telegram 健康检查。

        返回：
            True: 连接正常
            False: 连接断开，需要重连
        """
        if not self._started:
            return False

        if self.client is None:
            return False

        try:
            # 检查连接状态
            if not self.client.is_connected():
                logger.warning("[Telegram] heartbeat: 连接已断开")
                return False

            # 检查授权状态
            if not await self.client.is_user_authorized():
                logger.warning("[Telegram] heartbeat: 授权已失效")
                return False

            logger.debug("[Telegram] heartbeat: OK")
            return True

        except Exception as e:
            logger.warning(f"[Telegram] heartbeat 异常: {type(e).__name__}: {e}")
            return False

    async def stop(self):
        _set_state(ModuleState.DISCONNECTED)
        if self.client:
            try:
                await self.client.disconnect()
                logger.info("Telegram 已断开")
            except Exception as e:
                logger.warning(f"Telegram 断开异常: {e}")
        self._started = False
        _set_state(ModuleState.STOPPED)
