import asyncio
import base64
import imaplib
import email
import ipaddress
import re
import ssl
import time
from email.header import decode_header
from urllib.parse import urlparse

import os
import shutil

_plugin_dir = os.path.dirname(os.path.abspath(__file__))
_schema_local = os.path.join(_plugin_dir, "_conf_schema.json")
_schema_template = os.path.join(_plugin_dir, "_conf_schema.template.json")
if not os.path.exists(_schema_local) and os.path.exists(_schema_template):
    shutil.copy2(_schema_template, _schema_local)

try:
    import socks
    HAS_PYSOCKS = True
except ImportError:
    HAS_PYSOCKS = False

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig

IMAP_SERVERS = {
    "qq.com": "imap.qq.com",
    "163.com": "imap.163.com",
    "126.com": "imap.126.com",
    "yeah.net": "imap.yeah.net",
    "sina.com": "imap.sina.com",
    "sina.cn": "imap.sina.com",
    "sohu.com": "imap.sohu.com",
    "foxmail.com": "imap.qq.com",
    "gmail.com": "imap.gmail.com",
    "outlook.com": "imap-mail.outlook.com",
    "hotmail.com": "imap-mail.outlook.com",
    "live.com": "imap-mail.outlook.com",
    "live.cn": "imap-mail.outlook.com",
    "aliyun.com": "imap.aliyun.com",
    "139.com": "imap.139.com",
    "189.cn": "imap.189.cn",
}

FOREIGN_IMAP_SERVERS = {
    "imap.gmail.com",
    "imap-mail.outlook.com",
}

HELP_TEXT = (
    "邮件提醒插件 - 命令列表\n\n"
    "/mail_alert add <邮箱> <授权码> [IMAP服务器]\n"
    "  添加邮箱监控，自动检测IMAP服务器，验证连接后保存，自动绑定当前会话\n\n"
    "/mail_alert remove <邮箱>\n"
    "  移除邮箱监控（仅添加者可操作）\n\n"
    "/mail_alert list\n"
    "  列出当前用户已添加的所有邮箱\n\n"
    "/mail_alert check [邮箱]\n"
    "  手动触发检查未读邮件\n\n"
    "/mail_alert bind <邮箱>\n"
    "  绑定当前会话接收该邮箱的通知\n\n"
    "/mail_alert unbind <邮箱>\n"
    "  解绑当前会话\n\n"
    "/mail_alert filter add <邮箱> <类型> <值>\n"
    "  添加白名单过滤规则（类型: domain/address/subject）\n\n"
    "/mail_alert filter remove <邮箱> <类型> <值>\n"
    "  移除白名单过滤规则\n\n"
    "/mail_alert filter list <邮箱>\n"
    "  查看邮箱的过滤规则\n\n"
    "/mail_alert status\n"
    "  查看监控状态\n\n"
    "/mail_alert help\n"
    "  显示本帮助信息"
)


@register("astrbot_plugin_mail_alert", "Zhalslar", "邮箱未读邮件提醒插件", "1.0.2")
class MailAlertPlugin(Star):
    IMAP_TIMEOUT = 30
    MAX_FETCH_COUNT = 20
    CACHE_RETENTION_DAYS = 30
    MAX_MAILBOXES_PER_USER = 10
    MAX_MAILBOXES_TOTAL = 50
    MAX_FILTER_VALUE_LENGTH = 200
    MAX_FILTERS_PER_MAILBOX = 20

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._last_check_ts = None
        self._cron_job = None
        self._kv_lock = asyncio.Lock()
        self._checking = False
        self._imap_semaphore = asyncio.Semaphore(10)

    async def initialize(self):
        check_interval = self.config.get("check_interval", 5)
        if check_interval < 1:
            check_interval = 1
        self._cron_job = await self.context.cron_manager.add_basic_job(
            name="mail_alert_check",
            cron_expression=f"*/{check_interval} * * * *",
            handler=self._check_all_mailboxes,
            description="定时检查邮箱未读邮件",
        )
        logger.info(f"邮件提醒插件已启动，检查间隔: {check_interval} 分钟")
        await self._test_proxy_connectivity()

    async def _test_proxy_connectivity(self):
        """测试代理连接可达性，结果输出到控制台日志。"""
        proxy_url = self.config.get("imap_proxy", "")
        if not proxy_url:
            logger.info("未配置IMAP代理。")
            return
        if not HAS_PYSOCKS:
            logger.warning(f"已配置IMAP代理 {proxy_url}，但未安装PySocks库，代理将不会生效。")
            return
        proxy_info = self._parse_proxy_url(proxy_url)
        if not proxy_info:
            logger.warning(f"IMAP代理地址格式无效: {proxy_url}")
            return
        proxy_type, proxy_host, proxy_port = proxy_info
        loop = asyncio.get_running_loop()
        try:
            import socket
            def _test_connection():
                sock = socket.create_connection((proxy_host, proxy_port), timeout=10)
                sock.close()
            await loop.run_in_executor(None, _test_connection)
            logger.info(f"IMAP代理连接测试成功: {proxy_url}")
        except Exception as e:
            logger.warning(f"IMAP代理连接测试失败: {proxy_url}，错误: {e}")

    async def terminate(self):
        if hasattr(self, "_cron_job") and self._cron_job:
            await self.context.cron_manager.delete_job(self._cron_job.job_id)
        logger.info("邮件提醒插件已停止")

    # ==================== 工具方法 ====================

    def _encode_password(self, pwd: str) -> str:
        # 注意：base64 仅为编码，不提供加密保护
        return base64.b64encode(pwd.encode()).decode()

    def _decode_password(self, encoded: str) -> str:
        # 注意：base64 仅为编码，不提供加密保护
        return base64.b64decode(encoded.encode()).decode()

    def _is_valid_email(self, addr: str) -> bool:
        if "@" not in addr:
            return False
        parts = addr.split("@")
        if len(parts) != 2:
            return False
        local, domain = parts
        if not local or not domain:
            return False
        if "." not in domain:
            return False
        return True

    def _is_safe_hostname(self, hostname: str) -> bool:
        """检查主机名是否安全（防止SSRF）"""
        try:
            ipaddress.ip_address(hostname)
            return False
        except ValueError:
            pass
        if "." not in hostname:
            return False
        if hostname.lower() in ("localhost", "localhost.localdomain"):
            return False
        return True

    def _parse_proxy_url(self, proxy_url: str):
        """解析代理URL，返回 (proxy_type, host, port) 或 None。"""
        try:
            parsed = urlparse(proxy_url)
            scheme = parsed.scheme.lower()
            host = parsed.hostname
            port = parsed.port
            if not host or not port:
                return None
            if scheme == "socks5":
                proxy_type = socks.SOCKS5
            elif scheme == "socks4":
                proxy_type = socks.SOCKS4
            elif scheme in ("http", "https"):
                proxy_type = socks.HTTP
            else:
                return None
            return (proxy_type, host, port)
        except Exception:
            return None

    def _create_imap_connection(self, imap_server: str, timeout: int):
        """创建IMAP连接，对国外服务器自动使用代理。"""
        proxy_url = self.config.get("imap_proxy", "")
        if proxy_url and HAS_PYSOCKS and imap_server in FOREIGN_IMAP_SERVERS:
            proxy_info = self._parse_proxy_url(proxy_url)
            if proxy_info:
                proxy_type, proxy_host, proxy_port = proxy_info
                sock = socks.create_connection(
                    (imap_server, 993),
                    timeout=timeout,
                    proxy_type=proxy_type,
                    proxy_addr=proxy_host,
                    proxy_port=proxy_port,
                )
                ssl_context = ssl.create_default_context()
                ssl_sock = ssl_context.wrap_socket(sock, server_hostname=imap_server)
                mail = imaplib.IMAP4_SSL(host=imap_server, ssl_context=ssl_context)
                mail.sock = ssl_sock
                mail.file = mail.sock.makefile('rb')
                # 读取服务器问候语
                mail._get_response()
                return mail
        return imaplib.IMAP4_SSL(imap_server, timeout=timeout)

    def _extract_email_from_header(self, from_header: str) -> str:
        match = re.search(r'<([^>]+)>', from_header)
        if match:
            return match.group(1).strip()
        match = re.search(r'[\w.\-+]+@[\w.\-]+', from_header)
        if match:
            return match.group(0).strip()
        return from_header.strip()

    def _detect_imap_server(self, email_addr: str) -> str:
        domain = email_addr.split("@")[-1].lower()
        return IMAP_SERVERS.get(domain, "")

    def _verify_imap_connection(self, email_addr: str, password: str, imap_server: str) -> str:
        """验证IMAP连接，成功返回空字符串，失败返回错误信息。"""
        mail = None
        try:
            mail = self._create_imap_connection(imap_server, self.IMAP_TIMEOUT)
            mail.login(email_addr, password)
            return ""
        except Exception as e:
            return str(e)
        finally:
            if mail:
                try:
                    mail.logout()
                except Exception:
                    pass

    def _mask_password(self, password: str) -> str:
        if len(password) <= 6:
            return "****"
        return password[:2] + "****" + password[-2:]

    def _decode_header(self, header_str: str) -> str:
        if not header_str:
            return ""
        decoded_parts = decode_header(header_str)
        result = []
        for part, charset in decoded_parts:
            if isinstance(part, bytes):
                try:
                    result.append(part.decode(charset or "utf-8", errors="replace"))
                except (LookupError, UnicodeDecodeError):
                    result.append(part.decode("utf-8", errors="replace"))
            else:
                result.append(part)
        return "".join(result)

    def _match_filters(self, from_addr: str, subject: str, filters: list) -> bool:
        extracted_email = self._extract_email_from_header(from_addr).lower()
        extracted_domain = extracted_email.split("@")[-1] if "@" in extracted_email else ""
        subject_lower = subject.lower()
        for f in filters:
            if f["type"] == "domain" and f["value"].lower() in extracted_domain:
                return True
            if f["type"] == "address" and f["value"].lower() == extracted_email:
                return True
            if f["type"] == "subject" and f["value"].lower() in subject_lower:
                return True
        return False

    def _imap_fetch_unread(self, mailbox_config: dict) -> list:
        """阻塞式IMAP获取未读邮件，在线程执行器中运行。"""
        results = []
        mail = None
        try:
            raw_password = self._decode_password(mailbox_config["password"])
            mail = self._create_imap_connection(mailbox_config["imap_server"], self.IMAP_TIMEOUT)
            mail.login(mailbox_config["email"], raw_password)
            status, select_data = mail.select("INBOX", readonly=True)
            uidvalidity = select_data[0].decode() if select_data and select_data[0] else "0"
            status, data = mail.uid('search', None, 'UNSEEN')
            if status != "OK":
                return results
            mail_ids = data[0].split()
            for mid in mail_ids[-self.MAX_FETCH_COUNT:]:
                try:
                    status, msg_data = mail.uid('fetch', mid, '(BODY.PEEK[HEADER.FIELDS (Subject From Date)])')
                    if status != "OK":
                        continue
                    if not msg_data or not msg_data[0] or not isinstance(msg_data[0], tuple) or len(msg_data[0]) < 2:
                        continue
                    msg = email.message_from_bytes(msg_data[0][1])
                    subject = self._decode_header(msg["Subject"])
                    from_addr = self._decode_header(msg["From"])
                    date_str = msg.get("Date", "")
                    uid = f"{mailbox_config['email']}:{uidvalidity}:{mid.decode()}"
                    if mailbox_config.get("filters"):
                        if not self._match_filters(from_addr, subject, mailbox_config["filters"]):
                            continue
                    results.append({
                        "uid": uid,
                        "subject": subject,
                        "from": from_addr,
                        "date": date_str,
                    })
                except Exception as e:
                    logger.warning(f"解析邮件 {mid} 失败: {e}")
                    continue
        except Exception as e:
            logger.error(f"IMAP check failed for {mailbox_config['email']}: {e}")
        finally:
            if mail:
                try:
                    mail.logout()
                except Exception:
                    pass
        return results

    async def _check_mailbox(self, mailbox_config: dict) -> list:
        async with self._imap_semaphore:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self._imap_fetch_unread, mailbox_config)

    async def _check_all_mailboxes(self):
        if self._checking:
            return
        self._checking = True
        try:
            # 在锁内读取数据，然后立即释放锁
            async with self._kv_lock:
                mailboxes = await self.get_kv_data("mailboxes", [])
            if not mailboxes:
                return
            # IMAP 操作在锁外执行，不阻塞用户命令
            tasks = [self._check_mailbox(mb) for mb in mailboxes]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            # 重新获取锁，读取最新缓存并写回
            pending_notifications = []
            async with self._kv_lock:
                notified_cache = await self.get_kv_data("notified_cache", {})
                current_mailboxes = await self.get_kv_data("mailboxes", [])
                now = time.time()
                changed = False
                cooldown = self.config.get("cooldown", 24) * 3600
                # 用 email 作为 key 构建最新 session 映射
                session_map = {mb["email"]: mb.get("sessions", []) for mb in current_mailboxes}
                for mb, result in zip(mailboxes, results):
                    if isinstance(result, Exception):
                        logger.error(f"检查邮箱 {mb['email']} 失败: {result}")
                        continue
                    sessions = session_map.get(mb["email"], [])
                    for mail_info in result:
                        uid = mail_info["uid"]
                        if uid in notified_cache and (now - notified_cache[uid]) < cooldown:
                            continue
                        msg_text = (
                            f"📬 新邮件提醒\n"
                            f"邮箱: {mb['email']}\n"
                            f"发件人: {mail_info['from']}\n"
                            f"主题: {mail_info['subject']}\n"
                            f"时间: {mail_info['date']}"
                        )
                        for session_str in sessions:
                            pending_notifications.append((session_str, msg_text))
                        notified_cache[uid] = now
                        changed = True
                cutoff = now - self.CACHE_RETENTION_DAYS * 24 * 3600
                old_keys = [k for k, v in notified_cache.items() if v < cutoff]
                for k in old_keys:
                    del notified_cache[k]
                    changed = True
                if changed:
                    await self.put_kv_data("notified_cache", notified_cache)
            # 在锁外发送通知
            for session_str, msg_text in pending_notifications:
                try:
                    chain = MessageChain().message(msg_text)
                    await self.context.send_message(session_str, chain)
                except Exception as e:
                    logger.error(f"发送邮件通知到 {session_str} 失败: {e}")
            self._last_check_ts = time.time()
        finally:
            self._checking = False

    # ==================== 指令组 ====================

    @filter.command_group("mail_alert")
    def mail_alert(self):
        """邮件提醒管理"""

    @mail_alert.command("add")
    async def add_mailbox(self, event: AstrMessageEvent, email_addr: str, password: str, imap_server: str = ""):
        """添加邮箱监控"""
        yield event.plain_result("收到指令，正在处理中...")
        yield event.plain_result("⚠️ 请尽快撤回上条包含授权码的消息。")
        if email_addr.lower().endswith("@gmail.com"):
            yield event.plain_result("提示: Gmail授权码自带空格（格式: XXXX XXXX XXXX XXXX），请确保已删除所有空格后再输入，否则会导致指令识别失败。")
        if not self._is_valid_email(email_addr):
            yield event.plain_result("邮箱地址格式不正确，请检查后重试。")
            return
        if not imap_server:
            imap_server = self._detect_imap_server(email_addr)
        if not imap_server:
            yield event.plain_result(f"无法自动检测 {email_addr} 的IMAP服务器，请手动指定。")
            return
        if imap_server != self._detect_imap_server(email_addr):
            if not self._is_safe_hostname(imap_server):
                yield event.plain_result("IMAP服务器地址不合法，请使用有效的域名。")
                return
        yield event.plain_result("正在测试IMAP连接，请稍候...")
        loop = asyncio.get_running_loop()
        err = await loop.run_in_executor(None, self._verify_imap_connection, email_addr, password, imap_server)
        if err:
            logger.error(f"IMAP verification failed for {email_addr}: {err}")
            yield event.plain_result(f"IMAP连接验证失败: {err}\n请检查邮箱地址、授权码和IMAP服务器是否正确。")
            return
        async with self._kv_lock:
            mailboxes = await self.get_kv_data("mailboxes", [])
            sender_id = event.get_sender_id()
            user_count = sum(1 for mb in mailboxes if mb["added_by"] == sender_id)
            if user_count >= self.MAX_MAILBOXES_PER_USER:
                result_msg = f"每个用户最多添加 {self.MAX_MAILBOXES_PER_USER} 个邮箱。"
            elif len(mailboxes) >= self.MAX_MAILBOXES_TOTAL:
                result_msg = f"系统邮箱总数已达上限 {self.MAX_MAILBOXES_TOTAL}。"
            elif any(mb["email"] == email_addr for mb in mailboxes):
                result_msg = f"邮箱 {email_addr} 已存在。"
            else:
                session_str = event.unified_msg_origin
                mailboxes.append({
                    "email": email_addr,
                    "password": self._encode_password(password),
                    "imap_server": imap_server,
                    "added_by": sender_id,
                    "sessions": [session_str],
                    "filters": [],
                })
                await self.put_kv_data("mailboxes", mailboxes)
                result_msg = f"邮箱 {email_addr} 添加成功，IMAP服务器: {imap_server}，已绑定当前会话。"
        yield event.plain_result(result_msg)

    @mail_alert.command("remove")
    async def remove_mailbox(self, event: AstrMessageEvent, email_addr: str):
        """移除邮箱监控"""
        sender_id = event.get_sender_id()
        async with self._kv_lock:
            mailboxes = await self.get_kv_data("mailboxes", [])
            result_msg = f"未找到邮箱 {email_addr}。"
            for i, mb in enumerate(mailboxes):
                if mb["email"] == email_addr:
                    if mb["added_by"] != sender_id:
                        result_msg = "只有添加者才能移除该邮箱。"
                    else:
                        mailboxes.pop(i)
                        await self.put_kv_data("mailboxes", mailboxes)
                        result_msg = f"邮箱 {email_addr} 已移除。"
                    break
        yield event.plain_result(result_msg)

    @mail_alert.command("list")
    async def list_mailboxes(self, event: AstrMessageEvent):
        """列出已添加的邮箱"""
        sender_id = event.get_sender_id()
        mailboxes = await self.get_kv_data("mailboxes", [])
        user_mailboxes = [mb for mb in mailboxes if mb["added_by"] == sender_id]
        if not user_mailboxes:
            yield event.plain_result("你还没有添加任何邮箱。")
            return
        lines = []
        for idx, mb in enumerate(user_mailboxes, 1):
            lines.append(f"[{idx}] {mb['email']}")
            try:
                masked = self._mask_password(self._decode_password(mb['password']))
            except Exception:
                masked = "****"
            lines.append(f"  授权码: {masked}")
            lines.append(f"  IMAP: {mb['imap_server']}")
            sessions_str = ", ".join(mb.get("sessions", [])) or "无"
            lines.append(f"  绑定会话: {sessions_str}")
            filters = mb.get("filters", [])
            if filters:
                filter_strs = [f"{f['type']}:{f['value']}" for f in filters]
                lines.append(f"  过滤规则: {', '.join(filter_strs)}")
            else:
                lines.append("  过滤规则: 无")
        yield event.plain_result("\n".join(lines))

    @mail_alert.command("check")
    async def check_mailbox(self, event: AstrMessageEvent, email_addr: str = ""):
        """手动检查未读邮件"""
        yield event.plain_result("收到指令，正在检查邮箱...")
        sender_id = event.get_sender_id()
        mailboxes = await self.get_kv_data("mailboxes", [])
        if email_addr:
            targets = [mb for mb in mailboxes if mb["email"] == email_addr and mb["added_by"] == sender_id]
            if not targets:
                yield event.plain_result(f"未找到你添加的邮箱 {email_addr}。")
                return
        else:
            targets = [mb for mb in mailboxes if mb["added_by"] == sender_id]
            if not targets:
                yield event.plain_result("你还没有添加任何邮箱。")
                return
        all_results = []
        all_uids = []
        for mb in targets:
            try:
                mails = await self._check_mailbox(mb)
                for m in mails:
                    all_uids.append(m["uid"])
                    all_results.append(
                        f"邮箱: {mb['email']}\n"
                        f"发件人: {m['from']}\n"
                        f"主题: {m['subject']}\n"
                        f"时间: {m['date']}"
                    )
            except Exception as e:
                all_results.append(f"邮箱 {mb['email']} 检查失败: {e}")
        if all_uids:
            async with self._kv_lock:
                notified_cache = await self.get_kv_data("notified_cache", {})
                now = time.time()
                for uid in all_uids:
                    notified_cache[uid] = now
                await self.put_kv_data("notified_cache", notified_cache)
        if not all_results:
            yield event.plain_result("没有未读邮件。")
        else:
            yield event.plain_result(f"共 {len(all_results)} 封未读邮件:\n\n" + "\n\n".join(all_results))

    @mail_alert.command("bind")
    async def bind_session(self, event: AstrMessageEvent, email_addr: str):
        """绑定当前会话接收通知"""
        sender_id = event.get_sender_id()
        session_str = event.unified_msg_origin
        async with self._kv_lock:
            mailboxes = await self.get_kv_data("mailboxes", [])
            result_msg = f"未找到你添加的邮箱 {email_addr}。"
            for mb in mailboxes:
                if mb["email"] == email_addr and mb["added_by"] == sender_id:
                    if session_str in mb.get("sessions", []):
                        result_msg = "当前会话已绑定该邮箱。"
                    else:
                        mb.setdefault("sessions", []).append(session_str)
                        await self.put_kv_data("mailboxes", mailboxes)
                        result_msg = f"已绑定当前会话到邮箱 {email_addr}。"
                    break
        yield event.plain_result(result_msg)

    @mail_alert.command("unbind")
    async def unbind_session(self, event: AstrMessageEvent, email_addr: str):
        """解绑当前会话"""
        sender_id = event.get_sender_id()
        session_str = event.unified_msg_origin
        async with self._kv_lock:
            mailboxes = await self.get_kv_data("mailboxes", [])
            result_msg = f"未找到你添加的邮箱 {email_addr}。"
            for mb in mailboxes:
                if mb["email"] == email_addr and mb["added_by"] == sender_id:
                    sessions = mb.get("sessions", [])
                    if session_str not in sessions:
                        result_msg = "当前会话未绑定该邮箱。"
                    else:
                        sessions.remove(session_str)
                        await self.put_kv_data("mailboxes", mailboxes)
                        result_msg = f"已解绑当前会话与邮箱 {email_addr}。"
                    break
        yield event.plain_result(result_msg)

    # ==================== filter 子指令组 ====================

    @mail_alert.group(sub_command="filter")
    def mail_filter(self):
        """过滤规则管理"""

    @mail_filter.command("add")
    async def filter_add(self, event: AstrMessageEvent, email_addr: str, filter_type: str, value: str):
        """添加白名单过滤规则"""
        if filter_type not in ("domain", "address", "subject"):
            yield event.plain_result("过滤类型必须是 domain、address 或 subject。")
            return
        if len(value) > self.MAX_FILTER_VALUE_LENGTH:
            yield event.plain_result(f"过滤值长度不能超过 {self.MAX_FILTER_VALUE_LENGTH} 个字符。")
            return
        sender_id = event.get_sender_id()
        async with self._kv_lock:
            mailboxes = await self.get_kv_data("mailboxes", [])
            result_msg = f"未找到你添加的邮箱 {email_addr}。"
            for mb in mailboxes:
                if mb["email"] == email_addr and mb["added_by"] == sender_id:
                    filters = mb.setdefault("filters", [])
                    found_dup = False
                    for f in filters:
                        if f["type"] == filter_type and f["value"] == value:
                            found_dup = True
                            break
                    if found_dup:
                        result_msg = "该过滤规则已存在。"
                    elif len(filters) >= self.MAX_FILTERS_PER_MAILBOX:
                        result_msg = f"每个邮箱最多添加 {self.MAX_FILTERS_PER_MAILBOX} 条过滤规则。"
                    else:
                        filters.append({"type": filter_type, "value": value})
                        await self.put_kv_data("mailboxes", mailboxes)
                        result_msg = f"已添加过滤规则: {filter_type}:{value}"
                    break
        yield event.plain_result(result_msg)

    @mail_filter.command("remove")
    async def filter_remove(self, event: AstrMessageEvent, email_addr: str, filter_type: str, value: str):
        """移除白名单过滤规则"""
        sender_id = event.get_sender_id()
        async with self._kv_lock:
            mailboxes = await self.get_kv_data("mailboxes", [])
            result_msg = f"未找到你添加的邮箱 {email_addr}。"
            for mb in mailboxes:
                if mb["email"] == email_addr and mb["added_by"] == sender_id:
                    filters = mb.get("filters", [])
                    found = False
                    for i, f in enumerate(filters):
                        if f["type"] == filter_type and f["value"] == value:
                            filters.pop(i)
                            await self.put_kv_data("mailboxes", mailboxes)
                            result_msg = f"已移除过滤规则: {filter_type}:{value}"
                            found = True
                            break
                    if not found:
                        result_msg = "未找到该过滤规则。"
                    break
        yield event.plain_result(result_msg)

    @mail_filter.command("list")
    async def filter_list(self, event: AstrMessageEvent, email_addr: str):
        """查看邮箱的过滤规则"""
        sender_id = event.get_sender_id()
        mailboxes = await self.get_kv_data("mailboxes", [])
        for mb in mailboxes:
            if mb["email"] == email_addr and mb["added_by"] == sender_id:
                filters = mb.get("filters", [])
                if not filters:
                    yield event.plain_result(f"邮箱 {email_addr} 没有设置过滤规则。")
                    return
                lines = [f"邮箱 {email_addr} 的过滤规则:"]
                for i, f in enumerate(filters, 1):
                    lines.append(f"  {i}. {f['type']}: {f['value']}")
                yield event.plain_result("\n".join(lines))
                return
        yield event.plain_result(f"未找到你添加的邮箱 {email_addr}。")

    # ==================== status / help ====================

    @mail_alert.command("status")
    async def show_status(self, event: AstrMessageEvent):
        """查看监控状态"""
        mailboxes = await self.get_kv_data("mailboxes", [])
        check_interval = self.config.get("check_interval", 5)
        if self._last_check_ts:
            last_check = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._last_check_ts))
        else:
            last_check = "尚未执行"
        lines = [
            "邮件提醒插件状态",
            f"监控邮箱数: {len(mailboxes)}",
            f"检查间隔: {check_interval} 分钟",
            f"上次检查: {last_check}",
        ]
        yield event.plain_result("\n".join(lines))

    @mail_alert.command("help")
    async def show_help(self, event: AstrMessageEvent):
        """显示帮助信息"""
        yield event.plain_result(HELP_TEXT)
