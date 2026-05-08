import asyncio
import inspect
import re
import sqlite3
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from quart import jsonify, request

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.star.filter.command import GreedyStr


PLUGIN_NAME = "astrbot_plugin_user_terms"
TERMS_INTERCEPT_PRIORITY = 1_000_000
STATUS_PENDING = "pending"
STATUS_ACCEPTED = "accepted"
STATUS_REJECTED = "rejected"
SAFE_SQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ALLOWED_SCHEMA_COLUMNS = {
    ("term_acceptances", "terms_revision"): "INTEGER NOT NULL DEFAULT 1",
}
DEFAULT_ACCEPT_PHRASE = "我已知晓并同意遵守条款所有内容"
DEFAULT_TERMS_TEXT = (
    "1. 使用机器人时请遵守所在平台与群组规则。\n"
    "2. 不要发送违法、侵权、骚扰或滥用内容。\n"
    "3. 管理员可根据实际情况停止服务或清理违规使用记录。"
)
DEFAULT_PROMPT_MESSAGE = (
    "请同意以下条款来得到使用权限：\n"
    "{terms}\n\n"
    "当前需要签署：{scopes}\n"
    "请阅读后回复“{accept_phrase}”；回复“拒绝”将无法继续使用。"
)
DEFAULT_PREMATURE_ACCEPT_MESSAGE = (
    "请先阅读以下用户条款，再回复“{accept_phrase}”完成签署：\n"
    "{terms}\n\n当前需要签署：{scopes}"
)
DEFAULT_REJECTED_MESSAGE = (
    "你已拒绝用户条款。必须同意条款后才可以使用；"
    "请阅读条款后回复“{accept_phrase}”以重新签署。"
)
DEFAULT_INVALID_MESSAGE = (
    "必须同意用户条款后才可以使用。请阅读条款后回复“{accept_phrase}”。"
)


@dataclass(frozen=True)
class TermsScope:
    scope_type: str
    scope_id: str
    signer_user_id: str

    @property
    def label(self) -> str:
        if self.scope_type == "group":
            return f"群组 {self.scope_id}"
        if self.scope_type == "disabled_group":
            return f"禁用群组 {self.scope_id}"
        if self.scope_type == "disabled_user":
            return f"禁用用户 {self.scope_id}"
        return f"用户 {self.scope_id}"


class UserTermsPlugin(Star):
    """用户条款签署控制。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context, config)
        self.config = config or {}
        self._migrate_legacy_config_defaults()
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.db_path = self.data_dir / "terms.sqlite3"
        self._init_db()
        self._sync_configured_targets()
        self._register_page_api(context)

    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            return self.config.get(key, default)
        except Exception:
            logger.error(
                f"{PLUGIN_NAME}: failed to read config {key!r}\n{traceback.format_exc()}",
            )
            return default

    def _set_cfg(self, key: str, value: Any) -> None:
        try:
            self.config[key] = value
        except Exception:
            logger.error(
                f"{PLUGIN_NAME}: failed to set config {key!r}\n{traceback.format_exc()}",
            )
            return

    def _migrate_legacy_config_defaults(self) -> None:
        legacy_accept = {self._normalize_action_text("同意")}
        current_accept = {
            self._normalize_action_text(item)
            for item in self._sid_list("accept_keywords")
        }
        if not current_accept or current_accept == legacy_accept:
            self._set_cfg("accept_keywords", [DEFAULT_ACCEPT_PHRASE])

        legacy_templates = {
            "prompt_message": (
                "请同意以下条款来得到使用权限：\n"
                "{terms}\n\n"
                "当前需要签署：{scopes}\n"
                "请回复“同意”；回复“拒绝”将无法继续使用。"
            ),
            "rejected_message": (
                "你已拒绝用户条款。必须同意条款后才可以使用；"
                "请回复“同意”以重新签署。"
            ),
            "invalid_message": "必须同意用户条款后才可以使用。请回复“同意”。",
        }
        new_templates = {
            "prompt_message": DEFAULT_PROMPT_MESSAGE,
            "rejected_message": DEFAULT_REJECTED_MESSAGE,
            "invalid_message": DEFAULT_INVALID_MESSAGE,
        }
        for key, legacy_template in legacy_templates.items():
            if self._cfg(key) == legacy_template:
                self._set_cfg(key, new_templates[key])

    def _init_db(self) -> None:
        with self._db() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS protected_targets (
                    scope_type TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (scope_type, scope_id)
                )
                """,
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS term_acceptances (
                    scope_type TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    signer_user_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    prompt_count INTEGER NOT NULL DEFAULT 0,
                    first_seen_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL,
                    prompted_at INTEGER,
                    decided_at INTEGER,
                    signer_name TEXT,
                    platform_id TEXT,
                    group_id TEXT,
                    message_origin TEXT,
                    last_message TEXT,
                    terms_revision INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY (scope_type, scope_id, signer_user_id)
                )
                """,
            )
            self._ensure_column(
                conn,
                "term_acceptances",
                "terms_revision",
                "INTEGER NOT NULL DEFAULT 1",
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS terms_revisions (
                    revision INTEGER PRIMARY KEY,
                    terms_text TEXT NOT NULL,
                    note TEXT,
                    created_at INTEGER NOT NULL
                )
                """,
            )
            self._ensure_initial_terms_revision(conn)
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_term_acceptances_status
                ON term_acceptances (scope_type, scope_id, status)
                """,
            )

    def _ensure_column(
        self,
        conn: sqlite3.Connection,
        table: str,
        column: str,
        ddl: str,
    ) -> None:
        self._validate_schema_column(table, column, ddl)
        columns = {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    @staticmethod
    def _validate_schema_column(table: str, column: str, ddl: str) -> None:
        if not SAFE_SQL_IDENTIFIER_RE.fullmatch(table):
            raise ValueError(f"unsafe sqlite table identifier: {table!r}")
        if not SAFE_SQL_IDENTIFIER_RE.fullmatch(column):
            raise ValueError(f"unsafe sqlite column identifier: {column!r}")
        if ALLOWED_SCHEMA_COLUMNS.get((table, column)) != ddl:
            raise ValueError(
                f"unexpected sqlite schema migration: {table}.{column} {ddl}",
            )

    def _ensure_initial_terms_revision(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT revision FROM terms_revisions ORDER BY revision DESC LIMIT 1",
        ).fetchone()
        if row is not None:
            return

        conn.execute(
            """
            INSERT INTO terms_revisions (revision, terms_text, note, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                1,
                str(self._cfg("terms_text", DEFAULT_TERMS_TEXT)),
                "初始条款",
                self._now(),
            ),
        )

    def _register_page_api(self, context: Context) -> None:
        register_web_api = getattr(context, "register_web_api", None)
        if not callable(register_web_api):
            return

        try:
            register_web_api(
                f"/{PLUGIN_NAME}/status",
                self.page_status,
                ["GET"],
                "User terms status",
            )
            register_web_api(
                f"/{PLUGIN_NAME}/publish",
                self.page_publish_terms,
                ["POST"],
                "Publish new terms and reset acceptance",
            )
        except Exception as exc:
            logger.error(f"{PLUGIN_NAME}: register page api failed: {exc!r}")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _sync_configured_targets(self) -> None:
        now = self._now()
        with self._db() as conn:
            for scope_type, key in (
                ("user", "user_sids"),
                ("group", "group_sids"),
                ("disabled_user", "disabled_user_sids"),
                ("disabled_group", "disabled_group_sids"),
                ("admin", "admin_sids"),
            ):
                for scope_id in self._sid_values(key):
                    conn.execute(
                        """
                        INSERT INTO protected_targets (
                            scope_type, scope_id, status, created_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(scope_type, scope_id) DO UPDATE SET
                            updated_at = excluded.updated_at
                        """,
                        (scope_type, scope_id, STATUS_PENDING, now, now),
                    )

    @filter.event_message_type(
        filter.EventMessageType.ALL,
        priority=TERMS_INTERCEPT_PRIORITY,
    )
    async def enforce_terms(self, event: AstrMessageEvent):
        """拦截未签署条款的受控用户或群组会话。"""
        if not self._cfg("enabled", True):
            return

        if not self._is_user_addressing_bot(event):
            return

        disabled_scopes = self._disabled_scopes(event)
        if disabled_scopes:
            text = self._render_template(
                self._cfg(
                    "disabled_message",
                    "该用户或群组已被禁用，请联系开发者处理。",
                ),
                disabled_scopes,
                event,
            )
            yield await self._stop_result(event, text)
            return

        scopes = self._applicable_scopes(event)
        if not scopes:
            return

        try:
            self._ensure_acceptance_rows(scopes, event)
            states = self._acceptance_states(scopes)
            current_revision = self._current_terms_revision()
        except Exception as exc:
            logger.error(f"{PLUGIN_NAME}: SQLite state check failed: {exc!r}")
            return

        unsigned_scopes = [
            scope
            for scope in scopes
            if states[scope]["status"] != STATUS_ACCEPTED
            or states[scope]["terms_revision"] != current_revision
        ]
        if not unsigned_scopes:
            return

        has_prompted = all(states[scope]["prompt_count"] > 0 for scope in unsigned_scopes)
        action = self._message_action(event.message_str)
        if action == "accept":
            if not has_prompted:
                self._mark_prompted(unsigned_scopes, event)
                text = self._render_template(
                    self._cfg(
                        "premature_accept_message",
                        DEFAULT_PREMATURE_ACCEPT_MESSAGE,
                    ),
                    unsigned_scopes,
                    event,
                )
                yield await self._stop_result(event, text)
                return

            self._set_status(unsigned_scopes, event, STATUS_ACCEPTED)
            text = self._render_template(
                self._cfg(
                    "accepted_message",
                    "已记录你对 {scopes} 的用户条款同意状态，现在可以继续使用。",
                ),
                unsigned_scopes,
                event,
            )
            yield await self._stop_result(event, text)
            return

        if action == "reject":
            self._set_status(unsigned_scopes, event, STATUS_REJECTED)
            text = self._render_template(
                self._cfg(
                    "rejected_message",
                    DEFAULT_REJECTED_MESSAGE,
                ),
                unsigned_scopes,
                event,
            )
            yield await self._stop_result(event, text)
            return

        template_key = "invalid_message" if has_prompted else "prompt_message"
        default_text = DEFAULT_INVALID_MESSAGE if has_prompted else DEFAULT_PROMPT_MESSAGE
        self._mark_prompted(unsigned_scopes, event)
        text = self._render_template(
            self._cfg(template_key, default_text),
            unsigned_scopes,
            event,
        )
        yield await self._stop_result(event, text)

    @filter.command_group(
        "terms",
        alias={"条款管理", "用户条款管理"},
        priority=TERMS_INTERCEPT_PRIORITY,
    )
    def terms(self):
        """用户条款管理指令组。"""
        pass

    @terms.command("help", alias={"帮助"}, priority=TERMS_INTERCEPT_PRIORITY)
    async def terms_help(self, event: AstrMessageEvent):
        """查看用户条款管理指令。"""
        yield await self._stop_result(event, self._admin_guarded(event, self._admin_help))

    @terms.command("status", alias={"list", "查看", "状态"}, priority=TERMS_INTERCEPT_PRIORITY)
    async def terms_status(
        self,
        event: AstrMessageEvent,
        scope_type: str = "all",
        scope_id: str = "",
        signer_user_id: str = "",
    ):
        """查看用户或群组的条款签署状态。"""
        yield await self._stop_result(
            event,
            self._admin_guarded(
                event,
                lambda: self._admin_status([scope_type, scope_id, signer_user_id]),
            ),
        )

    @terms.command("set", alias={"修改"}, priority=TERMS_INTERCEPT_PRIORITY)
    async def terms_set(
        self,
        event: AstrMessageEvent,
        scope_type: str,
        scope_id: str,
        signer_or_status: str,
        status: str = "",
    ):
        """修改用户或群组的条款签署状态。"""
        args = [scope_type, scope_id, signer_or_status]
        if status:
            args.append(status)
        yield await self._stop_result(
            event,
            self._admin_guarded(event, lambda: self._admin_set_status(args, event)),
        )

    @terms.command("reset", alias={"重置"}, priority=TERMS_INTERCEPT_PRIORITY)
    async def terms_reset(
        self,
        event: AstrMessageEvent,
        scope_type: str = "all",
        scope_id: str = "",
        signer_user_id: str = "",
    ):
        """重置用户或群组的条款签署状态。"""
        yield await self._stop_result(
            event,
            self._admin_guarded(
                event,
                lambda: self._admin_reset([scope_type, scope_id, signer_user_id]),
            ),
        )

    @terms.command("publish", alias={"发布"}, priority=TERMS_INTERCEPT_PRIORITY)
    async def terms_publish(self, event: AstrMessageEvent, terms_text: GreedyStr):
        """发布新条款并重置已有签署记录。"""
        yield await self._stop_result(
            event,
            self._admin_guarded(
                event,
                lambda: self._admin_publish(str(terms_text).strip(), "聊天指令发布"),
            ),
        )

    def _applicable_scopes(self, event: AstrMessageEvent) -> list[TermsScope]:
        sender_id = self._clean_sid(event.get_sender_id())
        if not sender_id:
            return []

        scopes: list[TermsScope] = []
        user_candidates = self._user_candidates(event)
        group_candidates = self._group_candidates(event)

        if not event.is_private_chat():
            matched_group = self._first_configured_match("group_sids", group_candidates)
            if matched_group:
                if self._group_is_currently_accepted(matched_group):
                    return []
                scopes.append(
                    TermsScope(
                        scope_type="group",
                        scope_id=matched_group,
                        signer_user_id=matched_group,
                    ),
                )
            return scopes

        if user_candidates & self._sid_values("user_sids"):
            scopes.append(
                TermsScope(
                    scope_type="user",
                    scope_id=sender_id,
                    signer_user_id=sender_id,
                ),
            )
        return scopes

    def _disabled_scopes(self, event: AstrMessageEvent) -> list[TermsScope]:
        sender_id = self._clean_sid(event.get_sender_id())
        if not sender_id:
            return []

        scopes: list[TermsScope] = []
        user_candidates = self._user_candidates(event)
        group_candidates = self._group_candidates(event)

        if user_candidates & self._sid_values("disabled_user_sids"):
            scopes.append(
                TermsScope(
                    scope_type="disabled_user",
                    scope_id=sender_id,
                    signer_user_id=sender_id,
                ),
            )

        matched_group = self._first_configured_match(
            "disabled_group_sids",
            group_candidates,
        )
        if matched_group:
            scopes.append(
                TermsScope(
                    scope_type="disabled_group",
                    scope_id=matched_group,
                    signer_user_id=sender_id,
                ),
            )

        return scopes

    def _user_candidates(self, event: AstrMessageEvent) -> set[str]:
        candidates: list[Any] = [event.get_sender_id()]
        if event.is_private_chat():
            candidates.extend(
                [
                    event.get_session_id(),
                    getattr(event, "session_id", ""),
                    getattr(event, "unified_msg_origin", ""),
                ],
            )
        return self._clean_sid_set(candidates)

    def _group_candidates(self, event: AstrMessageEvent) -> set[str]:
        group_id = self._clean_sid(event.get_group_id())
        if not group_id:
            return set()

        candidates = [
            group_id,
            event.get_session_id(),
            getattr(event, "session_id", ""),
            getattr(event, "unified_msg_origin", ""),
        ]
        return self._clean_sid_set(candidates)

    def _first_configured_match(self, key: str, candidates: set[str]) -> str:
        for sid in self._sid_values(key):
            if sid in candidates:
                return sid
        return ""

    def _group_is_currently_accepted(self, group_id: str) -> bool:
        current_revision = self._current_terms_revision()
        with self._db() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM term_acceptances
                WHERE scope_type = 'group'
                  AND scope_id = ?
                  AND status = ?
                  AND terms_revision = ?
                LIMIT 1
                """,
                (group_id, STATUS_ACCEPTED, current_revision),
            ).fetchone()
        return row is not None

    def _sid_values(self, key: str) -> set[str]:
        return set(self._sid_list(key))

    def _sid_list(self, key: str) -> list[str]:
        raw = self._cfg(key, [])
        values: list[Any]
        if isinstance(raw, str):
            values = re.split(r"[\n,;，；]+", raw)
        elif isinstance(raw, (list, tuple, set)):
            values = list(raw)
        else:
            values = []
        cleaned: list[str] = []
        for value in values:
            sid = self._clean_sid(value)
            if sid and sid not in cleaned:
                cleaned.append(sid)
        return cleaned

    def _clean_sid_set(self, values: list[Any]) -> set[str]:
        cleaned: set[str] = set()
        for value in values:
            sid = self._clean_sid(value)
            if sid:
                cleaned.add(sid)
        return cleaned

    @staticmethod
    def _clean_sid(value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip().strip("'\"").strip()

    def _ensure_acceptance_rows(
        self,
        scopes: list[TermsScope],
        event: AstrMessageEvent,
    ) -> None:
        now = self._now()
        terms_revision = self._current_terms_revision()
        with self._db() as conn:
            for scope in scopes:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO term_acceptances (
                        scope_type, scope_id, signer_user_id, status, prompt_count,
                        first_seen_at, last_seen_at, signer_name, platform_id,
                        group_id, message_origin, last_message, terms_revision
                    )
                    VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        scope.scope_type,
                        scope.scope_id,
                        scope.signer_user_id,
                        STATUS_PENDING,
                        now,
                        now,
                        event.get_sender_name(),
                        event.get_platform_id(),
                        event.get_group_id(),
                        event.unified_msg_origin,
                        event.message_str,
                        terms_revision,
                    ),
                )
                conn.execute(
                    """
                    UPDATE term_acceptances
                    SET last_seen_at = ?,
                        signer_name = ?,
                        platform_id = ?,
                        group_id = ?,
                        message_origin = ?,
                        last_message = ?
                    WHERE scope_type = ?
                      AND scope_id = ?
                      AND signer_user_id = ?
                    """,
                    (
                        now,
                        event.get_sender_name(),
                        event.get_platform_id(),
                        event.get_group_id(),
                        event.unified_msg_origin,
                        event.message_str,
                        scope.scope_type,
                        scope.scope_id,
                        scope.signer_user_id,
                    ),
                )

    def _acceptance_states(self, scopes: list[TermsScope]) -> dict[TermsScope, sqlite3.Row]:
        states: dict[TermsScope, sqlite3.Row] = {}
        with self._db() as conn:
            for scope in scopes:
                row = conn.execute(
                    """
                    SELECT status, prompt_count, terms_revision
                    FROM term_acceptances
                    WHERE scope_type = ?
                      AND scope_id = ?
                      AND signer_user_id = ?
                    """,
                    (scope.scope_type, scope.scope_id, scope.signer_user_id),
                ).fetchone()
                if row is None:
                    states[scope] = {
                        "status": STATUS_PENDING,
                        "prompt_count": 0,
                        "terms_revision": self._current_terms_revision(),
                    }  # type: ignore[assignment]
                else:
                    states[scope] = row
        return states

    def _set_status(
        self,
        scopes: list[TermsScope],
        event: AstrMessageEvent,
        status: str,
    ) -> None:
        now = self._now()
        terms_revision = self._current_terms_revision()
        with self._db() as conn:
            for scope in scopes:
                conn.execute(
                    """
                    UPDATE term_acceptances
                    SET status = ?,
                        decided_at = ?,
                        last_seen_at = ?,
                        signer_name = ?,
                        platform_id = ?,
                        group_id = ?,
                        message_origin = ?,
                        last_message = ?,
                        terms_revision = ?
                    WHERE scope_type = ?
                      AND scope_id = ?
                      AND signer_user_id = ?
                    """,
                    (
                        status,
                        now,
                        now,
                        event.get_sender_name(),
                        event.get_platform_id(),
                        event.get_group_id(),
                        event.unified_msg_origin,
                        event.message_str,
                        terms_revision,
                        scope.scope_type,
                        scope.scope_id,
                        scope.signer_user_id,
                    ),
                )

    def _mark_prompted(
        self,
        scopes: list[TermsScope],
        event: AstrMessageEvent,
    ) -> None:
        now = self._now()
        with self._db() as conn:
            for scope in scopes:
                conn.execute(
                    """
                    UPDATE term_acceptances
                    SET prompt_count = prompt_count + 1,
                        prompted_at = ?,
                        last_seen_at = ?,
                        last_message = ?
                    WHERE scope_type = ?
                      AND scope_id = ?
                      AND signer_user_id = ?
                    """,
                    (
                        now,
                        now,
                        event.message_str,
                        scope.scope_type,
                        scope.scope_id,
                        scope.signer_user_id,
                    ),
                )

    def _is_user_addressing_bot(self, event: AstrMessageEvent) -> bool:
        if event.is_private_chat():
            return True

        if not event.get_group_id():
            return True

        if self._has_at_bot(event):
            return True

        if self._cfg("allow_wake_command_as_at", True) and getattr(
            event,
            "is_at_or_wake_command",
            False,
        ):
            return True

        return self._has_external_interaction_handler(event)

    def _has_at_bot(self, event: AstrMessageEvent) -> bool:
        self_id = self._clean_sid(event.get_self_id())
        if not self_id:
            return False

        for component in event.get_messages():
            if isinstance(component, Comp.At) and self._clean_sid(component.qq) == self_id:
                return True
        return False

    def _has_external_interaction_handler(self, event: AstrMessageEvent) -> bool:
        get_extra = getattr(event, "get_extra", None)
        if not callable(get_extra):
            return False

        try:
            handlers = get_extra("activated_handlers", default=[])
        except TypeError:
            handlers = get_extra("activated_handlers") or []
        except Exception:
            return False

        for handler in handlers or []:
            if self._is_own_handler(handler):
                continue
            if self._handler_has_interaction_filter(handler):
                return True
        return False

    @staticmethod
    def _is_own_handler(handler: Any) -> bool:
        module_path = str(getattr(handler, "handler_module_path", ""))
        handler_full_name = str(getattr(handler, "handler_full_name", ""))
        return PLUGIN_NAME in module_path or PLUGIN_NAME in handler_full_name

    @staticmethod
    def _handler_has_interaction_filter(handler: Any) -> bool:
        filters = list(getattr(handler, "event_filters", []) or [])
        if not filters:
            return False

        broad_filters = {
            "EventMessageTypeFilter",
            "PlatformAdapterTypeFilter",
            "PermissionTypeFilter",
        }
        for handler_filter in filters:
            filter_name = handler_filter.__class__.__name__
            if filter_name in broad_filters:
                continue
            return True
        return False

    def _message_action(self, text: str) -> str:
        normalized_text = self._normalize_action_text(text)
        if not normalized_text:
            return ""

        accept_keywords = self._accept_keywords()
        reject_keywords = {
            self._normalize_action_text(item)
            for item in self._sid_values("reject_keywords")
        }

        if normalized_text in accept_keywords:
            return "accept"
        if normalized_text in reject_keywords:
            return "reject"
        return ""

    def _accept_keywords(self) -> set[str]:
        raw_keywords = self._sid_list("accept_keywords")
        normalized_keywords = {
            self._normalize_action_text(item) for item in raw_keywords
        }
        legacy_default = {self._normalize_action_text("同意")}
        if not normalized_keywords or normalized_keywords == legacy_default:
            return {self._normalize_action_text(DEFAULT_ACCEPT_PHRASE)}
        return normalized_keywords

    def _primary_accept_phrase(self) -> str:
        raw_keywords = self._sid_list("accept_keywords")
        normalized_keywords = {
            self._normalize_action_text(item) for item in raw_keywords
        }
        legacy_default = {self._normalize_action_text("同意")}
        if not raw_keywords or normalized_keywords == legacy_default:
            return DEFAULT_ACCEPT_PHRASE
        return raw_keywords[0]

    def _is_terms_admin(self, event: AstrMessageEvent) -> bool:
        admin_sids = self._sid_values("admin_sids")
        if not admin_sids:
            return False
        return bool(self._user_candidates(event) & admin_sids)

    def _admin_guarded(self, event: AstrMessageEvent, action: Callable[[], str]) -> str:
        if not self._is_terms_admin(event):
            return "你没有权限使用条款管理指令。"
        return action()

    def _admin_publish(self, terms_text: str, note: str = "") -> str:
        if not terms_text:
            return "请提供新条款正文，例如：/terms publish 新条款正文"
        revision = self._publish_terms(terms_text, note)
        return f"已发布条款 v{revision}，并重置所有已记录签署状态。"

    @staticmethod
    def _admin_help() -> str:
        return (
            "条款管理指令：\n"
            "/terms status [user|group] [目标SID] [用户SID]\n"
            "/terms set user <用户SID> <accepted|pending|rejected>\n"
            "/terms set group <群组SID> <accepted|pending|rejected>\n"
            "/terms reset all\n"
            "/terms reset user <用户SID>\n"
            "/terms reset group <群组SID>\n"
            "/terms publish <新条款正文>"
        )

    def _admin_status(self, args: list[str]) -> str:
        scope_type = self._normalize_scope_type(args[0]) if len(args) >= 1 else ""
        scope_id = self._clean_sid(args[1]) if len(args) >= 2 else ""
        signer_user_id = self._clean_sid(args[2]) if len(args) >= 3 else ""
        if scope_type == "all":
            scope_type = ""
        if scope_type and scope_type not in {"user", "group"}:
            return "范围只能是 user 或 group。"

        rows = self._list_acceptances(scope_type, scope_id, signer_user_id, limit=20)
        payload = self._dashboard_payload()
        header = (
            f"当前条款版本：v{payload['current_revision']}\n"
            f"统计：已同意 {payload['stats'].get('accepted', 0)}，"
            f"待签署 {payload['stats'].get('pending', 0)}，"
            f"已拒绝 {payload['stats'].get('rejected', 0)}，"
            f"旧条款 {payload['stats'].get('pending_new_terms', 0)}"
        )
        if not rows:
            return f"{header}\n暂无匹配签署记录。"
        return f"{header}\n\n最近记录：\n" + "\n".join(
            self._format_acceptance_row(row) for row in rows
        )

    def _admin_set_status(
        self,
        args: list[str],
        event: AstrMessageEvent,
    ) -> str:
        if len(args) < 3:
            return "参数不足。示例：/terms set user 1919810 accepted"

        scope_type = self._normalize_scope_type(args[0])
        if scope_type not in {"user", "group"}:
            return "范围只能是 user 或 group。"

        if scope_type == "user":
            scope_id = self._clean_sid(args[1])
            signer_user_id = scope_id
            status = self._normalize_status(args[2])
        else:
            scope_id = self._clean_sid(args[1])
            signer_user_id = scope_id
            status_arg = args[3] if len(args) >= 4 else args[2]
            status = self._normalize_status(status_arg)

        if not scope_id or not signer_user_id:
            return "目标 SID 不能为空。"
        if not status:
            return "状态只能是 accepted、pending、rejected，或 同意、待签署、拒绝。"

        self._admin_upsert_acceptance(scope_type, scope_id, signer_user_id, status, event)
        target = (
            f"{self._scope_label(scope_type)} {scope_id}"
            if scope_type == "group"
            else f"{self._scope_label(scope_type)} {scope_id} / 用户 {signer_user_id}"
        )
        return (
            f"已将 {target} 设置为 {self._status_label(status)}。"
        )

    def _admin_reset(self, args: list[str]) -> str:
        if not args:
            return "参数不足。示例：/terms reset all"

        scope_type = self._normalize_scope_type(args[0])
        if scope_type == "all":
            count = self._reset_acceptances()
            return f"已重置全部签署记录，共 {count} 条。"
        if scope_type not in {"user", "group"}:
            return "范围只能是 all、user 或 group。"

        scope_id = self._clean_sid(args[1]) if len(args) >= 2 else ""
        signer_user_id = self._clean_sid(args[2]) if len(args) >= 3 else ""
        if not scope_id:
            return "请提供目标 SID。"

        count = self._reset_acceptances(scope_type, scope_id, signer_user_id)
        return f"已重置 {count} 条签署记录。"

    def _list_acceptances(
        self,
        scope_type: str = "",
        scope_id: str = "",
        signer_user_id: str = "",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if scope_type:
            clauses.append("scope_type = ?")
            params.append(scope_type)
        if scope_id:
            clauses.append("scope_id = ?")
            params.append(scope_id)
        if signer_user_id:
            clauses.append("signer_user_id = ?")
            params.append(signer_user_id)

        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        sql = f"""
            SELECT scope_type, scope_id, signer_user_id, status, prompt_count,
                   first_seen_at, last_seen_at, prompted_at, decided_at,
                   signer_name, platform_id, group_id, message_origin,
                   last_message, terms_revision
            FROM term_acceptances
            {where}
            ORDER BY last_seen_at DESC
            LIMIT ?
        """
        params.append(limit)
        current_revision = self._current_terms_revision()
        with self._db() as conn:
            return [
                self._acceptance_row_to_dict(row, current_revision)
                for row in conn.execute(sql, params).fetchall()
            ]

    def _admin_upsert_acceptance(
        self,
        scope_type: str,
        scope_id: str,
        signer_user_id: str,
        status: str,
        event: AstrMessageEvent,
    ) -> None:
        now = self._now()
        revision = self._current_terms_revision()
        decided_at = now if status in {STATUS_ACCEPTED, STATUS_REJECTED} else None
        with self._db() as conn:
            conn.execute(
                """
                INSERT INTO term_acceptances (
                    scope_type, scope_id, signer_user_id, status, prompt_count,
                    first_seen_at, last_seen_at, prompted_at, decided_at,
                    signer_name, platform_id, group_id, message_origin,
                    last_message, terms_revision
                )
                VALUES (?, ?, ?, ?, 0, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope_type, scope_id, signer_user_id) DO UPDATE SET
                    status = excluded.status,
                    last_seen_at = excluded.last_seen_at,
                    decided_at = excluded.decided_at,
                    signer_name = excluded.signer_name,
                    platform_id = excluded.platform_id,
                    group_id = excluded.group_id,
                    message_origin = excluded.message_origin,
                    last_message = excluded.last_message,
                    terms_revision = excluded.terms_revision
                """,
                (
                    scope_type,
                    scope_id,
                    signer_user_id,
                    status,
                    now,
                    now,
                    decided_at,
                    event.get_sender_name(),
                    event.get_platform_id(),
                    event.get_group_id(),
                    event.unified_msg_origin,
                    "管理员指令修改",
                    revision,
                ),
            )

    def _reset_acceptances(
        self,
        scope_type: str = "",
        scope_id: str = "",
        signer_user_id: str = "",
    ) -> int:
        clauses: list[str] = []
        params: list[Any] = [
            STATUS_PENDING,
            self._current_terms_revision(),
            self._now(),
        ]
        if scope_type:
            clauses.append("scope_type = ?")
            params.append(scope_type)
        if scope_id:
            clauses.append("scope_id = ?")
            params.append(scope_id)
        if signer_user_id:
            clauses.append("signer_user_id = ?")
            params.append(signer_user_id)

        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._db() as conn:
            cursor = conn.execute(
                f"""
                UPDATE term_acceptances
                SET status = ?,
                    prompt_count = 0,
                    prompted_at = NULL,
                    decided_at = NULL,
                    terms_revision = ?,
                    last_seen_at = ?
                {where}
                """,
                params,
            )
            return cursor.rowcount

    @staticmethod
    def _normalize_scope_type(value: str) -> str:
        mapping = {
            "用户": "user",
            "user": "user",
            "u": "user",
            "群": "group",
            "群组": "group",
            "group": "group",
            "g": "group",
            "全部": "all",
            "all": "all",
        }
        normalized = str(value or "").strip().lower()
        return mapping.get(normalized, normalized)

    @staticmethod
    def _normalize_status(value: str) -> str:
        mapping = {
            "accepted": STATUS_ACCEPTED,
            "accept": STATUS_ACCEPTED,
            "同意": STATUS_ACCEPTED,
            "已同意": STATUS_ACCEPTED,
            "pending": STATUS_PENDING,
            "待签署": STATUS_PENDING,
            "未签署": STATUS_PENDING,
            "rejected": STATUS_REJECTED,
            "reject": STATUS_REJECTED,
            "拒绝": STATUS_REJECTED,
            "已拒绝": STATUS_REJECTED,
        }
        return mapping.get(str(value or "").strip().lower(), "")

    @staticmethod
    def _scope_label(scope_type: str) -> str:
        return "群组" if scope_type == "group" else "用户"

    @staticmethod
    def _status_label(status: str) -> str:
        labels = {
            STATUS_ACCEPTED: "已同意",
            STATUS_PENDING: "待签署",
            STATUS_REJECTED: "已拒绝",
        }
        return labels.get(status, status)

    def _format_acceptance_row(self, row: dict[str, Any]) -> str:
        target = (
            f"群组 {row['scope_id']}"
            if row["scope_type"] == "group" and row["signer_user_id"] == row["scope_id"]
            else (
                f"{self._scope_label(row['scope_type'])} {row['scope_id']} / "
                f"用户 {row['signer_user_id']}"
            )
        )
        return (
            f"- {target}："
            f"{self._status_label(row['effective_status'])}，"
            f"条款 v{row['terms_revision']}，"
            f"最后触发 {self._format_timestamp(row['last_seen_at'])}"
        )

    @staticmethod
    def _format_timestamp(value: Any) -> str:
        if not value:
            return "-"
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(value)))

    @staticmethod
    def _normalize_action_text(text: Any) -> str:
        cleaned = "" if text is None else str(text)
        cleaned = re.sub(r"\s+", "", cleaned)
        return cleaned.strip().lower()

    def _render_template(
        self,
        template: str,
        scopes: list[TermsScope],
        event: AstrMessageEvent,
    ) -> str:
        text = str(template)
        replacements = {
            "terms": self._active_terms_text(),
            "terms_revision": str(self._current_terms_revision()),
            "scopes": "、".join(scope.label for scope in scopes),
            "accept_phrase": self._primary_accept_phrase(),
            "user_id": event.get_sender_id(),
            "user_name": event.get_sender_name(),
            "group_id": event.get_group_id(),
            "platform_id": event.get_platform_id(),
            "umo": event.unified_msg_origin,
        }
        for key, value in replacements.items():
            text = text.replace("{" + key + "}", value or "")
        return text

    def _current_terms_revision(self) -> int:
        with self._db() as conn:
            row = conn.execute(
                "SELECT revision FROM terms_revisions ORDER BY revision DESC LIMIT 1",
            ).fetchone()
        return int(row["revision"]) if row else 1

    def _active_terms_text(self) -> str:
        with self._db() as conn:
            row = conn.execute(
                """
                SELECT terms_text
                FROM terms_revisions
                ORDER BY revision DESC
                LIMIT 1
                """,
            ).fetchone()
        if row and row["terms_text"]:
            return str(row["terms_text"])
        return str(self._cfg("terms_text", DEFAULT_TERMS_TEXT))

    def _publish_terms(self, terms_text: str, note: str = "") -> int:
        terms = terms_text.strip() or self._active_terms_text()
        now = self._now()
        with self._db() as conn:
            row = conn.execute(
                "SELECT revision FROM terms_revisions ORDER BY revision DESC LIMIT 1",
            ).fetchone()
            next_revision = int(row["revision"]) + 1 if row else 1
            conn.execute(
                """
                INSERT INTO terms_revisions (revision, terms_text, note, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (next_revision, terms, note.strip(), now),
            )
            conn.execute(
                """
                UPDATE term_acceptances
                SET status = ?,
                    prompt_count = 0,
                    prompted_at = NULL,
                    decided_at = NULL,
                    terms_revision = ?,
                    last_seen_at = ?
                """,
                (STATUS_PENDING, next_revision, now),
            )
        return next_revision

    async def page_status(self):
        payload = await asyncio.to_thread(self._dashboard_payload)
        return jsonify(payload)

    async def page_publish_terms(self):
        if not request.is_json:
            return jsonify({"ok": False, "error": "request body must be JSON"}), 415

        try:
            body = await request.get_json()
        except Exception:
            logger.error(
                f"{PLUGIN_NAME}: failed to parse publish request JSON\n"
                f"{traceback.format_exc()}",
            )
            return jsonify({"ok": False, "error": "invalid JSON body"}), 400

        if not isinstance(body, dict):
            return jsonify({"ok": False, "error": "JSON body must be an object"}), 400

        terms_text = str(body.get("terms_text") or "").strip()
        note = str(body.get("note") or "").strip()
        if not terms_text:
            return jsonify({"ok": False, "error": "terms_text is required"}), 400

        revision = await asyncio.to_thread(self._publish_terms, terms_text, note)
        return jsonify(
            {
                "ok": True,
                "revision": revision,
                "message": "已发布新条款，并重置所有已记录签署状态。",
            },
        )

    def _dashboard_payload(self) -> dict[str, Any]:
        current_revision = self._current_terms_revision()
        with self._db() as conn:
            rows = [
                self._acceptance_row_to_dict(row, current_revision)
                for row in conn.execute(
                    """
                    SELECT scope_type, scope_id, signer_user_id, status, prompt_count,
                           first_seen_at, last_seen_at, prompted_at, decided_at,
                           signer_name, platform_id, group_id, message_origin,
                           last_message, terms_revision
                    FROM term_acceptances
                    ORDER BY last_seen_at DESC
                    LIMIT 500
                    """,
                ).fetchall()
            ]
            revisions = [
                {
                    "revision": int(row["revision"]),
                    "terms_text": str(row["terms_text"]),
                    "note": str(row["note"] or ""),
                    "created_at": int(row["created_at"]),
                }
                for row in conn.execute(
                    """
                    SELECT revision, terms_text, note, created_at
                    FROM terms_revisions
                    ORDER BY revision DESC
                    LIMIT 20
                    """,
                ).fetchall()
            ]

        stats = self._status_stats(rows)
        return {
            "ok": True,
            "plugin": PLUGIN_NAME,
            "current_revision": current_revision,
            "active_terms_text": self._active_terms_text(),
            "configured_targets": self._configured_target_summary(rows),
            "disabled_targets": self._disabled_target_summary(),
            "acceptances": rows,
            "revisions": revisions,
            "stats": stats,
        }

    def _acceptance_row_to_dict(
        self,
        row: sqlite3.Row,
        current_revision: int,
    ) -> dict[str, Any]:
        status = str(row["status"])
        revision = int(row["terms_revision"])
        effective_status = status
        if status == STATUS_ACCEPTED and revision != current_revision:
            effective_status = "pending_new_terms"

        return {
            "scope_type": str(row["scope_type"]),
            "scope_id": str(row["scope_id"]),
            "signer_user_id": str(row["signer_user_id"]),
            "status": status,
            "effective_status": effective_status,
            "prompt_count": int(row["prompt_count"]),
            "first_seen_at": int(row["first_seen_at"]),
            "last_seen_at": int(row["last_seen_at"]),
            "prompted_at": self._optional_int(row["prompted_at"]),
            "decided_at": self._optional_int(row["decided_at"]),
            "signer_name": str(row["signer_name"] or ""),
            "platform_id": str(row["platform_id"] or ""),
            "group_id": str(row["group_id"] or ""),
            "message_origin": str(row["message_origin"] or ""),
            "last_message": str(row["last_message"] or ""),
            "terms_revision": revision,
        }

    def _configured_target_summary(
        self,
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        targets: list[dict[str, Any]] = []
        for scope_type, key, disabled_key in (
            ("user", "user_sids", "disabled_user_sids"),
            ("group", "group_sids", "disabled_group_sids"),
        ):
            disabled_values = self._sid_values(disabled_key)
            for sid in sorted(self._sid_values(key)):
                scope_rows = [
                    row
                    for row in rows
                    if row["scope_type"] == scope_type and row["scope_id"] == sid
                ]
                targets.append(
                    {
                        "scope_type": scope_type,
                        "scope_id": sid,
                        "disabled": sid in disabled_values,
                        "total_records": len(scope_rows),
                        "stats": self._status_stats(scope_rows),
                    },
                )
        return targets

    def _disabled_target_summary(self) -> list[dict[str, str]]:
        targets: list[dict[str, str]] = []
        for scope_type, key in (
            ("user", "disabled_user_sids"),
            ("group", "disabled_group_sids"),
        ):
            for sid in sorted(self._sid_values(key)):
                targets.append({"scope_type": scope_type, "scope_id": sid})
        return targets

    @staticmethod
    def _status_stats(rows: list[dict[str, Any]]) -> dict[str, int]:
        stats = {
            "accepted": 0,
            "pending": 0,
            "rejected": 0,
            "pending_new_terms": 0,
        }
        for row in rows:
            status = str(row.get("effective_status") or row.get("status") or STATUS_PENDING)
            if status not in stats:
                stats[status] = 0
            stats[status] += 1
        return stats

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        if value is None:
            return None
        return int(value)

    @staticmethod
    async def _stop_result(event: AstrMessageEvent, text: str):
        result = event.plain_result(text)
        stop_event = getattr(event, "stop_event", None)
        if callable(stop_event):
            maybe_awaitable = stop_event()
            if inspect.isawaitable(maybe_awaitable):
                await maybe_awaitable
        result_stop_event = getattr(result, "stop_event", None)
        if callable(result_stop_event):
            maybe_awaitable = result_stop_event()
            if inspect.isawaitable(maybe_awaitable):
                await maybe_awaitable
        return result

    @staticmethod
    def _now() -> int:
        return int(time.time())
