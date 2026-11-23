"""Tkinter entry point for the activity ticketing prototype."""

from argparse import ArgumentParser
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sqlite3
import tkinter as tk
from tkinter import filedialog, ttk

from ticketing import AlreadyCheckedIn, RuleError, SessionDraft, SoldOut, TicketRequest, Ticketing

STATES = {
    "draft": "草稿", "pending": "待审核", "published": "已发布",
    "unlisted": "已下架", "disabled": "已停用", "cancelled": "已取消",
}
WAITLIST_STATES = {
    "queued": "候补中", "offered": "待确认", "confirmed": "已确认", "closed": "已关闭",
}
ROLE_HEADINGS = {
    "organizer": "活动管理", "admin": "活动审核与监管",
    "participant": "活动浏览与报名", "checker": "场次检票",
}
LOG_TARGET_NAMES = {
    "event": "活动", "session": "场次", "ticket_type": "票种", "registration": "报名",
    "waitlist": "候补", "ticket": "电子票",
}


class CardList(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.viewport = ttk.Frame(self)
        self.viewport.grid(row=0, column=0, sticky="nsew")
        self.body = ttk.Frame(self.viewport)
        self.body.place(x=0, y=0, relwidth=1)
        self.scrollbar = ttk.Scrollbar(
            self, orient="vertical", command=self.move_scrollbar,
        )
        self.offset = 0
        self.scrollbar_update = None
        self.viewport.bind("<Configure>", self.schedule_scrollbar_update)
        self.body.bind("<Configure>", self.schedule_scrollbar_update)

    def schedule_scrollbar_update(self, _event=None):
        if self.scrollbar_update is None:
            self.scrollbar_update = self.after_idle(self.update_scrollbar)

    def update_scrollbar(self):
        if self.scrollbar_update is not None:
            self.after_cancel(self.scrollbar_update)
            self.scrollbar_update = None
        height = self.viewport.winfo_height()
        content_height = max(height, self.body.winfo_reqheight())
        self.offset = min(max(0, self.offset), content_height - height)
        if self.body.winfo_y() != -self.offset:
            self.body.place_configure(y=-self.offset)
        self.scrollbar.set(
            self.offset / content_height,
            (self.offset + height) / content_height,
        )
        shown = bool(self.scrollbar.winfo_manager())
        if (content_height > height) != shown:
            if shown:
                self.scrollbar.grid_remove()
            else:
                self.scrollbar.grid(row=0, column=1, sticky="ns")

    def move_scrollbar(self, mode, value, unit=None):
        if mode == "moveto":
            self.offset = int(float(value) * self.body.winfo_reqheight())
        else:
            step = self.viewport.winfo_height() if unit == "pages" else 36
            self.offset += int(value) * step
        self.update_scrollbar()

    def scroll(self, delta):
        if not delta or not self.scrollbar.winfo_ismapped():
            return False
        self.move_scrollbar("scroll", -1 if delta > 0 else 1, "units")
        return True

    def clear(self):
        for widget in self.body.winfo_children():
            widget.destroy()
        self.offset = 0
        self.schedule_scrollbar_update()


class Desktop:
    def __init__(self, root, service):
        self.root = root
        self.service = service
        self.users = service.users()
        self.actor = self.users[0]
        self.sessions = []
        self.activities = {}
        self.participant_event_id = None
        self.activity_cards = {}
        self.filters = {}
        root.title("活动票务与报名系统")
        root.geometry("1030x640")
        root.minsize(760, 600)
        self.configure_styles()

        header = ttk.Frame(root, style="Header.TFrame", padding=(24, 16))
        header.pack(fill="x")
        calendar = tk.Canvas(
            header, width=30, height=30, highlightthickness=0,
            background=ttk.Style(root).lookup("Header.TFrame", "background"),
        )
        blue = "#70b1ff"
        calendar.create_rectangle(4, 6, 26, 27, outline=blue, width=2)
        calendar.create_line(4, 12, 26, 12, fill=blue, width=2)
        for x in (10, 20):
            calendar.create_line(x, 3, x, 9, fill=blue, width=2)
        for x in (10, 15, 20):
            for y in (17, 22):
                calendar.create_oval(
                    x - 1, y - 1, x + 1, y + 1, fill=blue, outline="",
                )
        calendar.pack(side="left", padx=(0, 10))
        ttk.Label(
            header, text="活动票务与报名系统", style="HeaderTitle.TLabel",
        ).pack(side="left")
        account_bar = ttk.Frame(header, style="Header.TFrame")
        account_bar.pack(side="right")
        ttk.Label(
            account_bar, text="演示身份", style="Header.TLabel",
        ).pack(side="left", padx=(0, 8))
        self.account = ttk.Combobox(
            account_bar, state="readonly", style="Choice.TCombobox",
            values=[row["name"] for row in self.users], width=15,
        )
        self.account.pack(side="left")
        self.account.current(0)
        self.account.bind("<<ComboboxSelected>>", self.switch_account)

        panel = ttk.Frame(root, style="Workspace.TFrame", padding=(24, 18))
        panel.pack(fill="both", expand=True)
        self.role_heading = ttk.Label(panel, style="Section.TLabel")
        self.role_heading.pack(anchor="w", pady=(0, 10))
        self.actions = ttk.Frame(panel, style="Workspace.TFrame")
        self.actions.pack(fill="x", pady=(0, 14))
        self.actions.bind("<Configure>", self.layout_actions)

        self.listing = ttk.Frame(panel, style="Surface.TFrame", padding=14)
        self.listing.pack(fill="both", expand=True)
        bar = ttk.Frame(self.listing, style="Surface.TFrame")
        bar.pack(fill="x", pady=(0, 12))
        self.list_heading = ttk.Label(bar, style="ListTitle.TLabel")
        self.list_heading.pack(side="left")
        ttk.Button(
            bar, text="搜索 / 刷新", style="Action.TButton",
            command=lambda: self.run(self.refresh),
        ).pack(
            side="right",
        )
        self.search = ttk.Entry(bar, width=22)
        self.search.pack(side="right", padx=(8, 6))
        self.search.bind("<Return>", lambda _: self.run(self.refresh))
        ttk.Label(bar, text="活动名称", style="Surface.TLabel").pack(side="right")

        lists = ttk.Frame(self.listing)
        lists.pack(fill="both", expand=True)
        self.activity_panel = ttk.Frame(lists)
        activity_table_frame = ttk.Frame(self.activity_panel)
        activity_table_frame.pack(fill="both", expand=True)
        activity_columns = (("title", "活动", 280), ("status", "状态", 104),
                            ("count", "场次数", 80), ("reason", "审核原因", 230))
        self.activity_table = ttk.Treeview(
            activity_table_frame,
            columns=[column for column, _, _ in activity_columns],
            show="headings", selectmode="browse", height=4,
        )
        for column, label, width in activity_columns:
            self.activity_table.heading(column, text=label, anchor="w")
            self.activity_table.column(
                column, width=width, stretch=column in ("title", "reason"),
            )
        activity_scroll = ttk.Scrollbar(
            activity_table_frame, orient="vertical",
            command=self.activity_table.yview,
        )
        activity_scroll.pack(side="right", fill="y")
        self.activity_table.configure(yscrollcommand=activity_scroll.set)
        self.activity_table.pack(side="left", fill="both", expand=True)
        self.activity_table.tag_configure("alternate", background="#f4f8fe")
        self.activity_table.bind(
            "<<TreeviewSelect>>", self.show_activity_sessions,
        )

        self.session_panel = ttk.Frame(lists)
        self.session_bar = ttk.Frame(self.session_panel)
        self.session_heading = ttk.Label(
            self.session_bar, style="ListTitle.TLabel",
        )
        self.session_heading.pack(side="left", fill="x", expand=True)
        self.add_session_button = ttk.Button(
            self.session_bar, text="添加场次",
            command=lambda: self.run(self.add_session), state="disabled",
        )
        self.edit_session_button = ttk.Button(
            self.session_bar, text="编辑场次",
            command=lambda: self.run(self.edit_draft_session),
            state="disabled",
        )
        table_frame = ttk.Frame(self.session_panel, style="Surface.TFrame")
        table_frame.pack(fill="both", expand=True)
        columns = ("title", "status", "venue", "start", "reason")
        self.table = ttk.Treeview(
            table_frame, columns=columns, show="headings", selectmode="browse",
            style="Sessions.Treeview",
        )
        scrollbar = ttk.Scrollbar(
            table_frame, orient="vertical", command=self.table.yview,
        )
        self.table.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.table.pack(side="left", fill="both", expand=True)
        self.table.bind("<<TreeviewSelect>>", self.update_checker_session)
        column_layout = (
            ("title", "活动", 220, 125),
            ("status", "状态", 104, 104),
            ("venue", "地点", 144, 80),
            ("start", "开始时间", 180, 180),
            ("reason", "审核原因", 142, 80),
        )
        for column, label, width, minimum in column_layout:
            self.table.heading(column, text=label, anchor="w")
            self.table.column(
                column, width=width, minwidth=minimum,
                stretch=width != minimum,
            )
        self.table.tag_configure("alternate", background="#f4f8fe")
        self.empty_label = ttk.Label(
            table_frame, text="暂无场次", style="Surface.TLabel",
        )

        self.participant_browser = ttk.Frame(lists)
        self.participant_browser.columnconfigure(0, minsize=205, weight=1)
        self.participant_browser.columnconfigure(1, minsize=330, weight=2)
        self.participant_browser.rowconfigure(1, weight=1)
        ttk.Label(
            self.participant_browser, text="活动列表", style="ListTitle.TLabel",
        ).grid(
            row=0, column=0, sticky="w", pady=(0, 8),
        )
        self.participant_detail_title = ttk.Label(
            self.participant_browser, style="ListTitle.TLabel",
        )
        self.participant_detail_title.grid(
            row=0, column=1, sticky="w", pady=(0, 8),
        )
        self.participant_activities = CardList(self.participant_browser)
        self.participant_activities.grid(
            row=1, column=0, sticky="nsew", padx=(0, 18),
        )
        self.participant_sessions = CardList(self.participant_browser)
        self.participant_sessions.grid(row=1, column=1, sticky="nsew")
        root.bind("<MouseWheel>", self.scroll_participant_cards, add="+")

        self.build_checker_panel(lists)

        self.status = tk.StringVar(master=root)
        ttk.Label(
            panel, textvariable=self.status, style="Status.TLabel",
        ).pack(anchor="w", pady=(8, 0))
        self.switch_account()
        self.root.after(15000, self.tick_deadlines)

    def build_checker_panel(self, parent):
        self.checker_panel = ttk.Frame(parent)
        self.checker_session_id = None
        self.verified_ticket = None
        self.checker_title = ttk.Label(
            self.checker_panel, style="ListTitle.TLabel",
        )
        self.checker_title.pack(anchor="w", pady=(0, 4))
        self.checker_details = ttk.Label(self.checker_panel, wraplength=340)
        self.checker_details.pack(anchor="w", pady=(0, 14))
        ttk.Label(
            self.checker_panel, text="电子票码",
        ).pack(anchor="w", pady=(0, 4))
        inputs = ttk.Frame(self.checker_panel)
        inputs.pack(fill="x")
        code_field = ttk.Frame(inputs)
        code_field.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.checker_code = tk.StringVar(master=self.root)
        self.code_entry = ttk.Entry(
            code_field, textvariable=self.checker_code, width=20,
            style="Form.TEntry",
        )
        self.code_entry.pack(fill="x")
        self.code_placeholder = ttk.Label(
            code_field, text="请输入或粘贴电子票码", style="CheckHint.TLabel",
        )
        self.code_placeholder.bind(
            "<Button-1>", lambda _: self.code_entry.focus_set(),
        )
        self.code_entry.bind("<FocusIn>", self.update_code_placeholder)
        self.code_entry.bind("<FocusOut>", self.update_code_placeholder)
        self.code_entry.bind("<Return>", lambda _: self.preview_ticket())
        self.verify_ticket_button = ttk.Button(
            inputs, text="核验", style="Primary.TButton",
            command=self.preview_ticket,
        )
        self.verify_ticket_button.pack(side="right")

        result = ttk.Frame(
            self.checker_panel, style="SessionCard.TFrame", padding=(12, 8),
        )
        result.pack(fill="x", pady=(12, 0))
        result.columnconfigure(0, weight=1)
        self.checker_result_title = ttk.Label(result, style="CardTitle.TLabel")
        self.checker_result_title.grid(
            row=0, column=0, columnspan=2, sticky="w",
        )
        self.checker_result_details = ttk.Label(
            result, style="CardDetail.TLabel", wraplength=200,
        )
        self.checker_result_details.grid(
            row=1, column=0, sticky="w", pady=(3, 0),
        )
        self.confirm_check_in_button = ttk.Button(
            result, text="确认签到", style="Primary.TButton",
            command=self.confirm_check_in, state="disabled",
        )
        self.confirm_check_in_button.grid(
            row=1, column=1, sticky="se", padx=(8, 0),
        )
        result.bind("<Configure>", self.fit_checker_result)
        self.checker_code.trace_add("write", self.reset_ticket_preview)
        self.reset_ticket_preview()

    def configure_styles(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        font = "Microsoft YaHei UI"
        workspace = "#eef4fb"
        navy = "#10284b"
        blue = "#155dcc"
        border = "#cbd9ee"
        style.configure("TFrame", background="white")
        style.configure(
            "TLabel", background="white", foreground="#385473",
            font=(font, 10),
        )
        style.configure(
            "TButton", font=(font, 10), padding=(10, 6), background="white",
                        foreground=navy, bordercolor=border)
        style.map(
            "TButton",
            background=[("pressed", "#dbeaff"), ("active", "#eaf2ff")],
        )
        style.configure(
            "TEntry", foreground=navy, fieldbackground="white",
            bordercolor=border,
        )
        style.configure(
            "Treeview", rowheight=27, font=(font, 10), foreground=navy,
                        background="white", fieldbackground="white",
                        bordercolor=border)
        style.configure(
            "Treeview.Heading", background="#e6eef9", foreground=navy,
                        font=(font, 10, "bold"), padding=(8, 6),
                        bordercolor=border)
        style.map(
            "Treeview.Heading",
            background=[("pressed", "#dbeaff"), ("active", "#eaf2ff")],
        )
        style.map(
            "Treeview", background=[("selected", blue)],
            foreground=[("selected", "white")],
        )
        style.configure(
            "Vertical.TScrollbar", background="#e6eef9", troughcolor="#f4f8fe",
                        bordercolor=border, arrowcolor=navy)
        style.configure("Header.TFrame", background=navy)
        style.configure(
            "HeaderTitle.TLabel", background=navy, foreground="white",
            font=(font, 21, "bold"),
        )
        style.configure(
            "Header.TLabel", background=navy, foreground="#e3edfc",
            font=(font, 10),
        )
        style.configure("Choice.TCombobox", padding=(8, 5), arrowsize=14)
        style.map("Choice.TCombobox", fieldbackground=[("readonly", "white")],
                  foreground=[("readonly", navy)],
                  selectbackground=[("readonly", "white")],
                  selectforeground=[("readonly", navy)])
        style.configure("Workspace.TFrame", background=workspace)
        style.configure(
            "Section.TLabel", background=workspace, foreground=navy,
            font=(font, 16, "bold"),
        )
        style.configure(
            "Status.TLabel", background=workspace, foreground="#385473",
            font=(font, 10),
        )
        style.configure("Surface.TFrame", background="white")
        style.configure(
            "Surface.TLabel", background="white", foreground="#385473",
            font=(font, 10),
        )
        style.configure("Form.TEntry", padding=(8, 5))
        style.configure(
            "ListTitle.TLabel", background="white", foreground=navy,
            font=(font, 12, "bold"),
        )
        style.configure(
            "Primary.TButton", font=(font, 10, "bold"), padding=(10, 6),
            background=blue,
                        foreground="white", bordercolor=blue)
        style.map(
            "Primary.TButton",
            background=[("pressed", "#104990"), ("active", "#1254b7")],
        )
        style.configure("Sessions.Treeview", rowheight=31)
        style.configure(
            "ActivityCard.TButton", anchor="w", justify="left",
            padding=(14, 13),
                        background="#f5f8fe", bordercolor=border,
                        font=(font, 11))
        style.configure(
            "SelectedActivityCard.TButton", anchor="w", justify="left",
            padding=(14, 13),
                        background="#e2edff", bordercolor=blue,
                        foreground=navy, font=(font, 11, "bold"))
        style.map(
            "SelectedActivityCard.TButton", background=[("active", "#d7e8ff")],
        )
        style.configure(
            "SessionCard.TFrame", background="#f5f8fe", bordercolor=border,
            relief="solid", borderwidth=1,
        )
        style.configure(
            "CardTitle.TLabel", background="#f5f8fe", foreground=navy,
            font=(font, 12, "bold"),
        )
        style.configure(
            "CardDetail.TLabel", background="#f5f8fe", foreground="#385473",
            font=(font, 10),
        )
        style.configure(
            "CheckError.TLabel", background="#f5f8fe", foreground="#a22c35",
            font=(font, 12, "bold"),
        )
        style.configure(
            "CheckSuccess.TLabel", background="#f5f8fe", foreground="#146840",
            font=(font, 12, "bold"),
        )
        style.configure(
            "CheckDuplicate.TLabel", background="#f5f8fe",
            foreground="#985000", font=(font, 12, "bold"),
        )
        style.configure(
            "CheckHint.TLabel", background="white", foreground="#556984",
            font=(font, 10),
        )

    def tick_deadlines(self):
        try:
            self.service.process_waitlists()
        except sqlite3.Error as exc:
            self.status.set(f"状态更新失败，稍后重试：{exc}")
        finally:
            self.root.after(15000, self.tick_deadlines)

    def run(self, action, *, parent=None):
        try:
            action()
        except (ValueError, InvalidOperation, OSError, sqlite3.Error) as exc:
            self.show_notice("操作失败", str(exc), parent=parent)

    def switch_account(self, _event=None):
        self.actor = self.users[self.account.current()]
        role = self.actor["role"]
        self.role_heading.configure(text=ROLE_HEADINGS[role])
        self.layout_role_lists(role)
        self.filters.clear()
        for widget in self.actions.winfo_children():
            widget.destroy()
        actions = {
            "organizer": [
                ("创建活动", self.create_event),
                ("提交审核", self.submit_review),
                ("提交变更", self.submit_change),
                ("取消场次 / 关闭票种", self.submit_closure),
                ("授权检票员", self.assign_checker),
                ("代取消报名", self.assisted_cancel),
                ("场次统计与导出", self.show_session_report),
                ("线下缴费", self.show_payments),
                ("管理活动", self.manage_event),
            ],
            "admin": [
                ("通过审核", lambda: self.review(True)),
                ("驳回审核", lambda: self.review(False)),
                ("代取消报名", self.assisted_cancel),
                ("管理活动", self.manage_event),
                ("操作日志", self.show_operation_logs),
            ],
            "participant": [
                ("我的电子票", self.show_tickets),
                ("我的候补", self.show_waitlist),
                ("我的通知", self.show_notices),
                ("筛选场次", self.filter_sessions),
            ],
            "checker": [],
        }
        for index, (label, action) in enumerate(actions[role]):
            ttk.Button(
                self.actions, text=label, width=-12,
                style="Primary.TButton" if index == 0 and role != "participant" else "Action.TButton",
                command=lambda callback=action: self.run(callback),
            )
        self.layout_actions()
        self.run(self.refresh)

    def layout_role_lists(self, role):
        for frame in (self.activity_panel, self.session_panel, self.session_bar, self.participant_browser,
                      self.checker_panel):
            frame.pack_forget()
        for button in (self.add_session_button, self.edit_session_button):
            button.pack_forget()

        self.listing.pack_configure(
            fill="x" if role == "checker" else "both",
            expand=role != "checker",
        )
        if role != "checker":
            self.checker_session_id = None
            self.checker_code.set("")
        if role == "participant":
            self.list_heading.configure(text="搜索活动")
            self.participant_browser.pack(fill="both", expand=True)
            return

        if role == "checker":
            self.list_heading.configure(text="已授权场次")
            self.table.configure(displaycolumns=("title", "start"), height=6)
            self.table.column("title", width=145)
            self.session_panel.pack(side="left", fill="both", padx=(0, 18))
            self.checker_panel.pack(side="left", fill="both", expand=True)
            return
        else:
            self.list_heading.configure(text="活动列表")
            self.table.configure(
                displaycolumns=("start", "venue", "status"), height=2,
            )
            self.table.heading("status", text="场次状态")
            self.activity_panel.pack(fill="both", expand=True)
            self.session_bar.pack(
                fill="x", before=self.table.master, pady=(14, 8),
            )
            if role == "organizer":
                self.add_session_button.pack(side="right")
                self.edit_session_button.pack(side="right", padx=(0, 8))
        self.session_panel.pack(fill="both", expand=True)

    def scroll_participant_cards(self, event):
        if self.actor["role"] != "participant":
            return
        for view in (self.participant_activities, self.participant_sessions):
            if str(event.widget).startswith(f"{view}.") and view.scroll(event.delta):
                return "break"

    def layout_actions(self, event=None):
        buttons = self.actions.winfo_children()
        if not buttons:
            return
        gap = 7
        available = event.width if event else self.actions.winfo_width()
        widest = max(button.winfo_reqwidth() for button in buttons)
        fit = max(1, available // (widest + gap))
        rows = (len(buttons) + fit - 1) // fit
        columns = (len(buttons) + rows - 1) // rows
        for index, button in enumerate(buttons):
            row, column = divmod(index, columns)
            button.grid(
                row=row, column=column, sticky="ew", padx=(0, gap),
                pady=(0, 5),
            )

    def refresh(self):
        sessions = self.service.list_sessions(
            self.actor["id"], keyword=self.search.get().strip(),
            **self.filters,
        )
        if self.actor["role"] != "checker":
            self.refresh_activities(sessions)
        else:
            self.render_sessions(sessions)

    @staticmethod
    def activity_status(row):
        if row["status"] == "pending" and row["is_public"]:
            return "变更待审核"
        return STATES[row["status"]]

    @staticmethod
    def session_label(row):
        return f"{row['starts_at'].replace('T', ' ')} · {row['venue']}"

    def refresh_activities(self, sessions):
        self.activities = {}
        for session in sessions:
            self.activities.setdefault(
                str(session["event_id"]), [],
            ).append(session)
        if self.actor["role"] == "participant":
            self.render_activity_cards()
            return

        previous = self.activity_table.selection()
        self.activity_table.delete(*self.activity_table.get_children())
        for index, (event_id, rows) in enumerate(self.activities.items()):
            activity = rows[0]
            self.activity_table.insert(
                "", "end", iid=event_id,
                tags=("alternate",) if index % 2 else (),
                values=(
                    activity["title"], self.activity_status(activity),
                    len(rows), activity["review_reason"] or "",
                ),
            )
        if self.activities:
            event_id = previous[0] if previous and previous[0] in self.activities else next(iter(self.activities))
            self.activity_table.selection_set(event_id)
            self.activity_table.see(event_id)
        self.show_activity_sessions()

    def render_activity_cards(self):
        self.participant_activities.clear()
        self.activity_cards.clear()
        if self.participant_event_id not in self.activities:
            self.participant_event_id = next(iter(self.activities), None)
        for event_id, rows in self.activities.items():
            card = ttk.Button(
                self.participant_activities.body,
                text=f"{rows[0]['title']}\n{len(rows)} 个场次",
                command=lambda key=event_id: self.run(lambda: self.show_participant_activity(key)),
            )
            card.pack(fill="x", pady=(0, 8))
            self.activity_cards[event_id] = card
        if not self.activities:
            ttk.Label(
                self.participant_activities.body, text="暂无符合条件的活动",
            ).pack(anchor="w", pady=8)
        self.show_participant_activity(self.participant_event_id)
        self.status.set(f"共 {len(self.activities)} 项活动")

    def show_participant_activity(self, event_id):
        rows = self.activities.get(event_id, [])
        stock = self.service.participant_session_stock(
            self.actor["id"], event_id,
        ) if rows else {}
        self.participant_event_id = event_id
        for key, card in self.activity_cards.items():
            style = "SelectedActivityCard.TButton" if key == event_id else "ActivityCard.TButton"
            card.configure(style=style)
        self.participant_sessions.clear()
        title = f"活动详情 · {rows[0]['title']}" if rows else "活动详情"
        self.participant_detail_title.configure(text=title)
        if not rows:
            ttk.Label(
                self.participant_sessions.body, text="暂无可显示的场次",
            ).pack(anchor="w", pady=8)
        now = self.service.clock()
        for index, row in enumerate(rows, start=1):
            self.add_session_card(
                index, row, stock.get(row["session_id"], 0), now,
            )

    def add_session_card(self, index, row, remaining, now):
        start = datetime.fromisoformat(row["starts_at"])
        period = start.strftime("%Y-%m-%d %H:%M")
        if row["ends_at"]:
            end = datetime.fromisoformat(row["ends_at"])
            end_text = end.strftime("%H:%M" if end.date() == start.date() else "%Y-%m-%d %H:%M")
            period = f"{period} – {end_text}"
        has_ticket_types = bool(row["open_ticket_types"])
        open_for_booking = has_ticket_types and now < start - timedelta(hours=2)
        card = ttk.Frame(
            self.participant_sessions.body, style="SessionCard.TFrame",
            padding=(14, 11),
        )
        card.pack(fill="x", pady=(0, 10))
        card.columnconfigure(0, weight=1)
        ttk.Label(
            card, text=f"场次 {index}", style="CardTitle.TLabel",
        ).grid(row=0, column=0, sticky="w")
        status = "报名已截止" if has_ticket_types else "暂无可售票种"
        if open_for_booking:
            status = "报名中" if remaining else "名额已满"
        ttk.Label(
            card, text=status, style="CardDetail.TLabel",
        ).grid(row=0, column=1, sticky="e")
        ttk.Label(card, text=period, style="CardDetail.TLabel").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(5, 3),
        )
        ttk.Label(
            card, text=row["venue"], style="CardDetail.TLabel",
        ).grid(row=2, column=0, columnspan=2, sticky="w")
        ttk.Label(
            card, text=f"剩余名额 {remaining} 人", style="CardDetail.TLabel",
        ).grid(
            row=3, column=0, sticky="w", pady=(6, 0),
        )
        ttk.Button(
            card, text="查看票种并报名", style="Primary.TButton",
            state="normal" if open_for_booking else "disabled",
            command=lambda: self.run(lambda: self.register(row)),
        ).grid(row=3, column=1, sticky="e", pady=(6, 0))

    def show_activity_sessions(self, _event=None):
        role = self.actor["role"]
        if role not in ("organizer", "admin"):
            return
        selection = self.activity_table.selection()
        rows = self.activities.get(selection[0], []) if selection else []
        heading = "场次管理"
        draft_actions = "disabled"
        if rows:
            activity = rows[0]
            self.session_heading.configure(text=f"{heading} · {activity['title']}")
            if role == "organizer" and activity["status"] == "draft":
                draft_actions = "normal"
        else:
            self.session_heading.configure(text=heading)
        self.add_session_button.configure(state=draft_actions)
        self.edit_session_button.configure(state=draft_actions)
        if rows is not self.sessions:
            self.render_sessions(rows)

    def render_sessions(self, sessions):
        selection = self.table.selection()
        selected_session = self.sessions[int(selection[0])]["session_id"] if selection else None
        self.sessions = sessions
        self.table.delete(*self.table.get_children())
        selected_index = 0
        for index, row in enumerate(self.sessions):
            self.table.insert(
                "", "end", iid=str(index),
                tags=("alternate",) if index % 2 else (),
                values=(
                    row["title"], "已取消" if row["cancelled_at"] else "正常",
                    row["venue"],
                    row["starts_at"].replace("T", " "),
                    row["review_reason"] or "",
                ),
            )
            if row["session_id"] == selected_session:
                selected_index = index
        if self.sessions:
            self.empty_label.place_forget()
            self.table.selection_set(str(selected_index))
            self.table.see(str(selected_index))
        else:
            self.empty_label.place(relx=0.5, rely=0.5, anchor="center")
        self.status.set(f"共 {len(self.sessions)} 个场次")
        self.update_checker_session()

    def filter_sessions(self):
        fields = self.form("筛选场次", [
            ("地点包含", self.filters.get("venue", "")),
            ("开始日期（年-月-日）", self.filters.get("date", "")),
            ("票种名称包含", self.filters.get("ticket", "")),
            ("仅看可报名且有余票的场次（填是或否）",
             "是" if self.filters.get("available") else "否"),
        ], message="留空表示不限。活动名称仍使用主窗口的搜索框。")
        if fields is None:
            return
        venue, date, ticket, remaining = (value.strip() for value in fields)
        if remaining not in ("", "是", "否"):
            raise RuleError("仅看有余票请填写「是」或「否」。")
        previous = self.filters
        self.filters = {
            "venue": venue,
            "date": date,
            "ticket": ticket,
            "available": remaining == "是",
        }
        try:
            self.refresh()
        except (ValueError, sqlite3.Error):
            self.filters = previous
            raise

    def selected(self):
        selection = self.table.selection()
        if not selection:
            raise RuleError("请先选择一个场次。")
        return self.sessions[int(selection[0])]

    def selected_activity(self):
        if self.actor["role"] not in ("organizer", "admin"):
            return self.selected()
        selection = self.activity_table.selection()
        if not selection:
            raise RuleError("请先选择一个活动。")
        return self.activities[selection[0]][0]

    def create_event(self):
        actor = self.actor["id"]
        window, panel, table = self.table_window("创建活动", (
            ("开始时间", 165), ("地点", 140), ("时长（分钟）", 95), ("容量", 75),
            ("票种数", 75),
        ), height=6)
        drafts = []
        saved = False
        header = ttk.Frame(panel)
        header.pack(fill="x", before=table.master, pady=(0, 8))
        ttk.Label(header, text="活动名称 *").pack(anchor="w", pady=(0, 3))
        name = ttk.Entry(header, style="Form.TEntry")
        name.pack(fill="x", pady=(0, 14))
        ttk.Label(
            header, text="场次与票种配置", style="ListTitle.TLabel",
        ).pack(side="left")
        tools = ttk.Frame(header)
        tools.pack(side="right")
        summary = tk.StringVar(master=window, value="共 0 个场次")
        ttk.Label(panel, textvariable=summary).pack(anchor="w", pady=(8, 12))

        def render(selected_index):
            table.delete(*table.get_children())
            for index, draft in enumerate(drafts):
                minutes = int((draft.ends_at - draft.starts_at).total_seconds() // 60)
                table.insert("", "end", iid=str(index), values=(
                    draft.starts_at.strftime("%Y-%m-%d %H:%M"), draft.venue,
                    minutes,
                    draft.capacity, len(draft.ticket_types),
                ))
            if drafts:
                item = str(min(selected_index, len(drafts) - 1))
                table.selection_set(item)
                table.see(item)
            summary.set(f"共 {len(drafts)} 个场次，总容量 {sum(draft.capacity for draft in drafts)} 人")

        def add():
            draft = self.session_form("添加场次", parent=window)
            if draft is not None:
                drafts.append(draft)
                render(len(drafts) - 1)

        def edit():
            selection = table.selection()
            if selection:
                index = int(selection[0])
                draft = self.session_form(
                    "编辑场次", parent=window, initial=drafts[index],
                )
                if draft is not None:
                    drafts[index] = draft
                    render(index)

        def remove():
            selection = table.selection()
            if selection:
                index = int(selection[0])
                drafts.pop(index)
                render(index)

        def save(submit):
            nonlocal saved
            try:
                self.service.create_event_with_sessions(
                    actor, name.get(), drafts, submit=submit,
                )
            except OverflowError as exc:
                raise RuleError("场次容量、票价或票种名额超出可用范围。") from exc
            saved = True
            window.destroy()

        ttk.Button(
            tools, text="添加场次", style="Primary.TButton", command=add,
        ).pack(side="left")
        edit_button = ttk.Button(
            tools, text="编辑", command=edit, state="disabled",
        )
        edit_button.pack(side="left", padx=6)
        remove_button = ttk.Button(
            tools, text="移除", command=remove, state="disabled",
        )
        remove_button.pack(side="left")

        def update_actions(_event=None):
            state = "normal" if table.selection() else "disabled"
            edit_button.configure(state=state)
            remove_button.configure(state=state)

        table.bind("<<TreeviewSelect>>", update_actions)
        buttons = ttk.Frame(panel)
        buttons.pack(anchor="e")
        ttk.Button(
            buttons, text="取消", command=window.destroy,
        ).pack(side="left")
        ttk.Button(
            buttons, text="保存草稿",
            command=lambda: self.run(lambda: save(False), parent=window),
        ).pack(
            side="left", padx=8,
        )
        ttk.Button(buttons, text="提交审核", style="Primary.TButton",
                   command=lambda: self.run(
                       lambda: save(True), parent=window,
                   )).pack(side="left")
        self.wait_for_modal(window, self.root, name)
        if saved:
            self.refresh()

    def add_session(self):
        event = self.selected_activity()
        if event["status"] != "draft":
            raise RuleError("请选择一条活动草稿，再添加场次。")
        actor = self.actor["id"]
        event_id = event["event_id"]

        def save(draft):
            self.service.add_session(
                actor, event_id, draft.venue, draft.starts_at, draft.capacity,
                draft.ticket_types,
                ends_at=draft.ends_at,
            )

        if self.session_form(
            "添加场次", message=f"活动：{event['title']}", on_submit=save,
        ) is not None:
            self.refresh()
            self.status.set(f"已为「{event['title']}」添加场次")

    def edit_draft_session(self):
        row = self.selected()
        actor, session = self.actor["id"], row["session_id"]
        draft = self.service.draft_session(actor, session)
        if self.session_form(
            "编辑草稿场次", initial=draft, message=f"活动：{row['title']}",
            on_submit=lambda values: self.service.edit_draft_session(actor, session, values),
        ) is not None:
            self.refresh()
            self.status.set(f"已保存「{row['title']}」的场次修改")

    def session_form(
        self, title, *, parent=None, initial=None, message="", on_submit=None,
    ):
        if initial is None:
            initial = SessionDraft(
                "", datetime.now() + timedelta(days=2), 30,
                (("普通票", 0, 20), ("VIP票", 2000, 10)),
            )
        minutes = int((initial.ends_at - initial.starts_at).total_seconds() // 60) if initial.ends_at else 120
        tickets = "\n".join(
            f"{name},{Decimal(price) / 100:.2f},{quota}" for name, price,
            quota in initial.ticket_types
        )
        fields = [
            ("地点 *", initial.venue),
            ("开始时间（年-月-日 时:分）*", initial.starts_at.strftime("%Y-%m-%d %H:%M")),
            ("预计时长（分钟）*", str(minutes)),
            ("场次容量 *", str(initial.capacity)),
            ("票种（名称,价格元,名额）*", tickets),
        ]
        draft = None

        def confirm(values):
            nonlocal draft
            parsed = self.parse_session(values)
            self.service.validate_session(parsed)
            if on_submit is not None:
                on_submit(parsed)
            draft = parsed

        self.form(
            title, fields, parent=parent, multiline_last=True,
            message=f"{message}\n带 * 的项目为必填项。" if message else "带 * 的项目为必填项。",
            multiline_hint="每行填一种票，用英文逗号分隔。例如：普通票,0,20",
            on_submit=confirm,
        )
        return draft

    @staticmethod
    def positive_integer(raw, label):
        try:
            value = int(raw)
        except ValueError as exc:
            raise RuleError(f"{label}必须是大于 0 的整数。") from exc
        if value < 1:
            raise RuleError(f"{label}必须是大于 0 的整数。")
        return value

    def parse_session(self, fields):
        venue, start, duration, capacity, raw_types = fields
        if not venue.strip():
            raise RuleError("请填写地点。")
        try:
            starts_at = datetime.strptime(start.strip(), "%Y-%m-%d %H:%M")
        except ValueError as exc:
            raise RuleError("请按「年-月-日 时:分」填写有效的开始时间。") from exc
        minutes = self.positive_integer(duration, "预计时长")
        seats = self.positive_integer(capacity, "场次容量")
        try:
            ends_at = starts_at + timedelta(minutes=minutes)
        except OverflowError as exc:
            raise RuleError("预计时长过长，请填写较小的分钟数。") from exc
        types = []
        for line_number, line in enumerate(raw_types.splitlines(), start=1):
            if not line.strip():
                continue
            parts = line.split(",")
            if len(parts) != 3 or not parts[0].strip():
                raise RuleError(f"第 {line_number} 行票种请按「名称,价格,名额」填写。")
            name, raw_price, raw_quota = parts
            try:
                price = self.parse_price(raw_price)
            except (InvalidOperation, RuleError) as exc:
                raise RuleError(f"第 {line_number} 行票价无效，最多保留两位小数。") from exc
            if price < 0:
                raise RuleError(f"第 {line_number} 行票价不能小于 0 元。")
            quota = self.positive_integer(raw_quota, f"第 {line_number} 行票种名额")
            types.append((name, price, quota))
        if not types:
            raise RuleError("请填写至少一种票。")
        return SessionDraft(
            venue.strip(), starts_at, seats, tuple(types), ends_at,
        )

    def submit_review(self):
        self.service.submit_review(
            self.actor["id"], self.selected_activity()["event_id"],
        )
        self.refresh()

    def manage_event(self):
        event = self.selected_activity()
        actor = self.actor
        option = "下架" if actor["role"] == "organizer" else "停用"
        if not event["is_public"]:
            raise RuleError("请选择一个仍对外开放的活动。")
        fields = self.form(
            "管理活动",
            [(f"操作（{option}或取消）", option), ("操作原因", "")],
            message=f"活动：{event['title']}",
        )
        if fields is None:
            return
        action, reason = fields
        actions = {option: "unlisted" if option == "下架" else "disabled", "取消": "cancelled"}
        if action not in actions or not reason.strip():
            raise RuleError(f"请输入「{option}」或「取消」，并填写原因。")
        consequence = (
            "未签到的报名和候补将取消，参与者会收到站内通知。"
            if action == "取消" else "活动将停止展示和新报名，已有报名保留。"
        )
        if not self.confirm_action(
            "确认活动操作", f"{action}「{event['title']}」？\n{consequence}",
        ):
            return
        self.service.set_event_status(
            actor["id"], event["event_id"], actions[action], reason,
        )
        self.refresh()
        self.status.set(f"已{action}「{event['title']}」")

    @staticmethod
    def parse_price(price):
        cents = Decimal(price.strip()) * 100
        if not cents.is_finite() or cents != cents.to_integral_value():
            raise RuleError("请填写有效票价，最多保留两位小数。")
        return int(cents)

    def submit_change(self):
        session = self.selected()["session_id"]
        actor = self.actor["id"]
        options = self.service.change_options(actor, session)
        if not options:
            raise RuleError("请选择本人已发布且没有待审变更的活动。")
        ticket = options[0]
        if len(options) > 1:
            choices = tuple(row["name"] for row in options)
            selected = self.form(
                "选择票种", [("本次要变更的票种", choices)], confirm_label="继续",
            )
            if selected is None:
                return
            ticket = next(row for row in options if row["name"] == selected[0])
        fields = self.form(
            "提交活动变更",
            [
                ("开始时间（年-月-日 时:分:秒）", ticket["starts_at"].replace("T", " ")),
                ("场次容量", str(ticket["session_capacity"])),
                ("票种容量", str(ticket["capacity"])),
                ("票价（元）", f"{ticket['price_cents'] / 100:.2f}"),
            ],
            message=f"票种：{ticket['name']}。审核期间继续使用原配置。",
        )
        if fields is None:
            return
        start, capacity, quota, price = fields
        self.service.submit_change(
            actor, session, ticket["id"], self.parse_price(price), int(quota),
            int(capacity), datetime.strptime(start, "%Y-%m-%d %H:%M:%S"),
        )
        self.refresh()
        self.status.set("变更已提交审核，原配置继续生效")

    def review(self, approved):
        event_id = self.selected_activity()["event_id"]
        actor = self.actor["id"]
        request = self.service.pending_review(actor, event_id)
        summary = ""
        if request["closure_session"] is not None:
            location = f"{request['closure_start'].replace('T', ' ')} · {request['closure_venue']}"
            if request["closure_ticket"] is None:
                summary = f"取消场次：{location}\n该场次未签到的报名和候补将取消，其他场次照常。"
            else:
                summary = f"关闭票种：{request['closure_name']}\n场次：{location}\n该票种的相关候补将取消。"
        elif request["session_id"] is not None:
            old_start = request["old_start"].replace("T", " ")
            start = request["starts_at"].replace("T", " ")
            summary = (
                f"开始时间：{old_start} → {start}\n"
                f"场次容量：{request['old_capacity']} → {request['session_capacity']}\n"
                f"{request['name']}容量：{request['old_quota']} → {request['ticket_capacity']}\n"
                f"票价：¥{request['old_price'] / 100:.2f} → ¥{request['price_cents'] / 100:.2f}"
            )
        if approved and summary and not self.confirm_action(
            "确认变更", summary + "\n\n是否通过并应用以上变更？",
        ):
            return
        reason = ""
        if not approved:
            fields = self.form("驳回审核", [("驳回原因", "")], message=summary)
            if fields is None:
                return
            reason = fields[0]
        self.service.review(
            actor, event_id, approved, reason,
            confirm_reduction=bool(summary), review_id=request["review_id"],
        )
        self.refresh()

    def submit_closure(self):
        selected = self.selected()
        actor, session = self.actor["id"], selected["session_id"]
        tickets = self.service.closure_options(actor, session)
        choices = {"取消所选场次": None}
        choices.update({f"关闭票种 · {row['name']}": row["id"] for row in tickets})
        fields = self.form(
            "提交取消或关闭申请", [("操作", tuple(choices))], confirm_label="提交审核",
            message=f"{selected['title']} · {self.session_label(selected)}\n管理员审核通过后生效。",
            on_submit=lambda values: self.service.submit_closure(
                actor, session, ticket_type=choices[values[0]],
            ),
        )
        if fields is not None:
            self.refresh()
            self.status.set("申请已提交，管理员审核通过后生效。")

    def assign_checker(self):
        session = self.selected()["session_id"]
        checker = next(row for row in self.users if row["role"] == "checker")
        self.service.assign_checker(self.actor["id"], checker["id"], session)
        self.status.set(f"已授权 {checker['name']} 核验所选场次")

    def register(self, selected):
        session = selected["session_id"]
        actor = self.actor["id"]
        types = self.service.ticket_types(actor, session)
        if not types:
            raise RuleError("该场次暂无可选票种。")
        options = {
            f"{row['name']} · ¥{row['price_cents'] / 100:.2f} · 余 {row['available']} 张": row["id"]
            for row in types
        }
        choices = tuple(options)
        no_second = "不添加第二张"
        requests = []

        def select_tickets(values):
            first, name, second, other_name = values
            selected_tickets = [TicketRequest(options[first], name)]
            if second != no_second:
                selected_tickets.append(TicketRequest(
                    options[second], other_name,
                ))
            elif other_name.strip():
                raise RuleError("填写第二位持票人后，请为第二张票选择票种。")
            requests.extend(self.service.validate_requests(selected_tickets))

        result = self.form("提交报名", [
            ("第一张票的票种", choices),
            ("第一张票的持票人", self.actor["name"]),
            ("第二张票的票种（选填）", (no_second, *choices)),
            ("第二张票的持票人（选填）", ""),
        ], message=f"{selected['title']} · {self.session_label(selected)}",
        on_submit=select_tickets)
        if result is None:
            return
        prices = {row["id"]: row["price_cents"] for row in types}
        try:
            codes = self.service.register(
                actor, session, requests, expected_prices=prices,
            )
        except SoldOut:
            if not self.confirm_action(
                "加入候补",
                "整单名额不足。开场前两小时半起不再发送新递补通知。是否加入候补？",
            ):
                return
            entry = self.service.join_waitlist(
                actor, session, requests, accept_no_offer=True,
                expected_prices=prices,
            )
            self.status.set(f"候补 #{entry} 已登记，可在「我的候补」查看")
            self.show_participant_activity(self.participant_event_id)
            return
        self.status.set(f"报名成功，已生成 {len(codes)} 张电子票")
        self.show_participant_activity(self.participant_event_id)
        self.show_tickets()

    def show_waitlist(self):
        actor = self.actor["id"]
        window = tk.Toplevel(self.root)
        window.title("我的候补")
        window.transient(self.root)
        frame = ttk.Frame(window, padding=16)
        frame.pack(fill="both", expand=True)
        columns = ("activity", "session", "types", "status", "deadline")
        table = ttk.Treeview(
            frame, columns=columns, show="headings", selectmode="browse",
        )
        for key, heading, width in zip(
            columns, ("活动", "场次", "票种", "状态", "确认截止"),
            (135, 275, 140, 85, 170),
        ):
            table.heading(key, text=heading)
            table.column(key, width=width)
        table.pack(fill="both", expand=True)
        rows = {}

        def refresh():
            data = self.service.my_waitlist(actor)
            selected = table.selection()
            table.delete(*table.get_children())
            rows.clear()
            for row in data:
                key = str(row["id"])
                rows[key] = row
                deadline = row["deadline"] if row["status"] == "offered" else None
                table.insert("", "end", iid=key, values=(
                    row["title"], self.session_label(row), row["types"],
                    WAITLIST_STATES[row["status"]],
                    datetime.fromisoformat(deadline).isoformat(
                        sep=" ", timespec="seconds",
                    )
                    if deadline else "",
                ))
            if selected and selected[0] in rows:
                table.selection_set(selected[0])

        def respond(accept):
            selected = table.selection()
            if not selected:
                raise RuleError("请先选中一条候补记录。")
            row = rows[selected[0]]
            if accept:
                if row["status"] != "offered":
                    raise RuleError("当前没有可确认的递补名额。")
                amount = Decimal(row["amount"]) / 100
                if not self.confirm_action(
                    "确认递补",
                    f"{row['title']} · {self.session_label(row)}\n{row['types']}\n合计 ¥{amount:.2f}",
                    parent=window,
                ):
                    return
            elif not self.confirm_action(
                "放弃候补", "是否放弃这笔候补？已预留的名额也会释放。", parent=window,
            ):
                return
            codes = self.service.respond_to_offer(
                actor, row["id"], accept=accept,
                expected_deadline=row["deadline"],
                expected_amount=row["amount"],
            )
            if accept:
                window.destroy()
                self.status.set(f"递补确认成功，已生成 {len(codes)} 张电子票")
                self.show_tickets()
            else:
                refresh()

        bar = ttk.Frame(frame)
        bar.pack(anchor="e", pady=(10, 0))
        for label, callback in (
            ("刷新", refresh), ("确认递补", lambda: respond(True)),
            ("放弃候补", lambda: respond(False)),
        ):
            ttk.Button(bar, text=label, command=lambda fn=callback: self.run(
                fn, parent=window,
            )).pack(side="left", padx=(5, 0))
        refresh()
        window.wait_visibility()
        window.grab_set()

    def show_tickets(self):
        actor = self.actor["id"]
        tickets = self.service.my_tickets(actor)
        if not tickets:
            self.show_notice("我的电子票", "当前没有电子票。")
            return
        window = tk.Toplevel(self.root)
        window.title("我的电子票")
        window.transient(self.root)
        frame = ttk.Frame(window, padding=16)
        frame.pack(fill="both", expand=True)
        display = tk.Text(
            frame, width=66, height=16, wrap="word",
            font=("Microsoft YaHei UI", 10),
                          foreground="#10284b", padx=10, pady=8, relief="flat",
                          highlightthickness=1,
                          highlightbackground="#cbd9ee",
                          highlightcolor="#155dcc")
        display.pack(fill="both", expand=True)
        now = self.service.clock()
        for ticket in tickets:
            status = ticket["registration_status"]
            checked_at = ticket["checked_at"]
            ends_at = ticket["ends_at"]
            if status == "cancelled":
                state = "已取消，票据失效"
            elif checked_at:
                state = checked_at.replace("T", " ")
            elif ticket["cancelled_at"] or ticket["event_status"] == "cancelled":
                state = "场次已取消，票据失效"
            elif status == "absent" or (ends_at and datetime.fromisoformat(ends_at) <= now):
                state = "缺席"
            else:
                state = "尚未签到"
            display.insert(
                "end",
                f"报名 #{ticket['registration_id']} · {ticket['title']}\n"
                f"场次 {self.session_label(ticket)}\n"
                f"{ticket['name']} · {ticket['holder_name']} · "
                f"票价 ¥{ticket['price_cents'] / 100:.2f} · {state}\n"
                f"{self.payment_label(ticket)} · 票码 {ticket['code']}\n\n",
            )
        display.configure(state="disabled")
        ttk.Button(
            frame,
            text="取消整单",
            command=lambda: self.run(lambda: self.cancel_my_order(window, actor), parent=window),
        ).pack(anchor="e", pady=(8, 0))
        window.wait_visibility()
        window.grab_set()

    def cancel_my_order(self, window, actor):
        fields = self.form(
            "取消整单", [("电子票上方显示的报名编号", "")], parent=window,
            on_submit=lambda values: self.positive_integer(values[0], "报名编号"),
        )
        if fields is None:
            return
        order = int(fields[0])
        if not self.confirm_action(
            "取消整单", f"取消报名 #{order} 后，这笔报名的所有票都会失效。继续吗？",
            parent=window,
        ):
            return
        self.service.cancel_registration(actor, order)
        window.destroy()
        self.refresh()
        self.status.set(f"报名 #{order} 已取消，名额已释放")
        self.show_tickets()

    def assisted_cancel(self):
        registrations = self.service.session_registrations(
            self.actor["id"], self.selected()["session_id"],
        )
        eligible = [row for row in registrations if row["status"] == "registered"]
        if not eligible:
            raise RuleError("所选场次没有未签到的有效报名。")
        choices = "\n".join(f"#{row['id']}  {row['name']}" for row in eligible)
        result = self.form(
            "代取消报名", [("报名编号", str(eligible[0]["id"])), ("代取消原因", "")],
            message=choices,
        )
        if result is None:
            return
        order = int(result[0])
        if order not in {row["id"] for row in eligible}:
            raise RuleError("请选择当前场次的有效报名编号。")
        self.service.cancel_registration(
            self.actor["id"], order, reason=result[1],
        )
        self.status.set(f"报名 #{order} 已人工取消")

    def update_checker_session(self, _event=None):
        if self.actor["role"] != "checker":
            return
        selection = self.table.selection()
        row = self.sessions[int(selection[0])] if selection else None
        session = row["session_id"] if row is not None else None
        if session != self.checker_session_id:
            self.checker_session_id = session
            self.checker_code.set("")
        self.checker_title.configure(text=row["title"] if row is not None else "尚未选择场次")
        self.checker_details.configure(
            text=self.session_label(row) if row is not None else "请从左侧选择已授权场次。",
        )
        state = "normal" if row is not None else "disabled"
        self.code_entry.configure(state=state)
        self.verify_ticket_button.configure(state=state)
        self.update_code_placeholder()

    def update_code_placeholder(self, _event=None):
        if (self.checker_code.get() or self.root.focus_get() == self.code_entry
                or self.code_entry.instate(("disabled",))):
            self.code_placeholder.place_forget()
        else:
            self.code_placeholder.place(x=10, rely=0.5, anchor="w")

    def fit_checker_result(self, event):
        width = event.width - self.confirm_check_in_button.winfo_reqwidth() - 34
        self.checker_result_details.configure(wraplength=max(100, width))

    def set_checker_result(self, title, detail, *, style="CardTitle.TLabel"):
        self.checker_result_title.configure(text=title, style=style)
        self.checker_result_details.configure(text=detail)

    def reset_ticket_preview(self, *_args):
        self.verified_ticket = None
        self.confirm_check_in_button.configure(state="disabled")
        self.update_code_placeholder()
        self.set_checker_result("待核验", "核验通过后可确认签到。")

    def show_duplicate_ticket(self, exc):
        self.confirm_check_in_button.configure(state="disabled")
        self.set_checker_result(
            "该电子票已完成签到",
            f"该票已于 {exc.first_checked_at} 完成首次签到，请勿重复操作。",
            style="CheckDuplicate.TLabel",
        )

    def preview_ticket(self):
        self.update_checker_session()
        self.reset_ticket_preview()
        session = self.checker_session_id
        if self.actor["role"] != "checker" or session is None:
            return
        code = self.checker_code.get().strip()
        if not code:
            self.set_checker_result(
                "请填写票码", "票码可从参与者的电子票复制。", style="CheckError.TLabel",
            )
            return
        try:
            name, ticket_type = self.service.preview_check_in(
                self.actor["id"], session, code,
            )
        except AlreadyCheckedIn as exc:
            self.show_duplicate_ticket(exc)
            return
        except (RuleError, sqlite3.Error) as exc:
            self.set_checker_result(
                "核验未通过", str(exc), style="CheckError.TLabel",
            )
            return
        self.verified_ticket = (session, code)
        self.set_checker_result("核验通过 · 待确认", f"持票人：{name}\n票种：{ticket_type}",
                                style="CheckSuccess.TLabel")
        self.confirm_check_in_button.configure(state="normal")

    def confirm_check_in(self):
        self.update_checker_session()
        if self.verified_ticket is None:
            return
        session, code = self.verified_ticket
        self.reset_ticket_preview()
        if (self.actor["role"] != "checker" or session != self.checker_session_id
                or code != self.checker_code.get().strip()):
            self.set_checker_result(
                "请重新核验", "场次或票码已变化。", style="CheckError.TLabel",
            )
            return
        try:
            name, timestamp = self.service.check_in(
                self.actor["id"], session, code,
            )
        except AlreadyCheckedIn as exc:
            self.show_duplicate_ticket(exc)
            return
        except (RuleError, sqlite3.Error) as exc:
            self.set_checker_result(
                "签到未完成", str(exc), style="CheckError.TLabel",
            )
            return
        self.checker_code.set("")
        self.set_checker_result(
            "签到成功", f"持票人：{name}\n签到时间：{timestamp.replace('T', ' ')}",
                                style="CheckSuccess.TLabel")
        self.code_entry.focus_set()

    @staticmethod
    def payment_label(row):
        if row["refunded_at"]:
            return "已退"
        if row["paid_at"]:
            return "已线下缴费"
        if row["amount"] == 0:
            return "无需缴费"
        return "待缴费"

    def table_window(self, title, columns, *, height=12):
        window = tk.Toplevel(self.root)
        window.title(title)
        window.transient(self.root)
        panel = ttk.Frame(window, padding=16)
        panel.pack(fill="both", expand=True)
        names = [name for name, _ in columns]
        table_panel = ttk.Frame(panel)
        table_panel.pack(fill="both", expand=True)
        table = ttk.Treeview(
            table_panel, columns=names, show="headings", selectmode="browse",
            height=height,
        )
        scrollbar = ttk.Scrollbar(
            table_panel, orient="vertical", command=table.yview,
        )
        table.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        table.pack(side="left", fill="both", expand=True)
        for name, width in columns:
            table.heading(name, text=name)
            table.column(name, width=width, minwidth=50, anchor="w")
        return window, panel, table

    def show_payments(self):
        actor, session = self.actor["id"], self.selected()["session_id"]
        rows = self.service.session_payments(actor, session)
        window, panel, table = self.table_window("线下缴费与退款", (
            ("报名编号", 90), ("报名人", 130), ("金额", 95),
            ("报名状态", 95), ("费用状态", 130),
        ))
        states = {"registered": "已报名", "checked_in": "已签到", "absent": "缺席", "cancelled": "已取消"}

        def render(records):
            table.delete(*table.get_children())
            for row in records:
                table.insert("", "end", iid=str(row["id"]), values=(
                    row["id"], row["name"], f"¥{row['amount'] / 100:.2f}",
                    states[row["status"]],
                    self.payment_label(row) if row["amount"] else "免费",
                ))

        def update(refund):
            selection = table.selection()
            if not selection:
                raise RuleError("请先选择一笔报名。")
            order = int(selection[0])
            verb = "退款" if refund else "缴费"
            if self.confirm_action(
                f"登记{verb}", f"确认线下已完成报名 #{order} 的整单{verb}？", parent=window,
            ):
                self.service.record_payment(actor, order, refund=refund)
                render(self.service.session_payments(actor, session))

        render(rows)
        buttons = ttk.Frame(panel)
        buttons.pack(anchor="e", pady=(10, 0))
        for name, refund in (("登记缴费", False), ("登记退款", True)):
            ttk.Button(
                buttons, text=name,
                command=lambda flag=refund: self.run(lambda: update(flag), parent=window),
            ).pack(side="left", padx=(5, 0))
        window.wait_visibility()
        window.grab_set()

    def show_notices(self):
        notices = self.service.my_notices(self.actor["id"])
        window, _, table = self.table_window(
            "我的通知", (("时间", 170), ("内容", 520)),
        )
        for row in notices:
            table.insert(
                "", "end",
                values=(row["created_at"].replace("T", " "), row["message"]),
            )
        window.wait_visibility()
        window.grab_set()

    def show_operation_logs(self):
        actor = self.actor["id"]
        window, panel, table = self.table_window("操作日志", (
            ("时间", 162), ("操作", 144), ("操作人", 105), ("操作对象", 112),
            ("关联活动", 145), ("原因", 164),
        ))
        filters = ttk.Frame(panel)
        filters.pack(fill="x", before=table.master, pady=(0, 10))
        fields = {}
        target_types = {"全部": "", **{name: kind for kind, name in LOG_TARGET_NAMES.items()}}
        labels = (("action", "操作"), ("operator", "操作人"), ("target_type", "对象类型"),
                  ("target", "对象编号"), ("date", "日期"))
        for key, name in labels:
            ttk.Label(filters, text=name).pack(side="left")
            if key == "target_type":
                widget = ttk.Combobox(
                    filters, state="readonly", values=tuple(target_types),
                    width=7,
                )
                widget.current(0)
            else:
                widget = ttk.Entry(filters, width=11 if key == "date" else 9)
            widget.pack(side="left", padx=(4, 8))
            fields[key] = widget

        def search():
            query = {key: field.get().strip() for key, field in fields.items()}
            query["target_type"] = target_types[query["target_type"]]
            logs = self.service.operation_history(actor, **query)
            table.delete(*table.get_children())
            for row in logs:
                kind = LOG_TARGET_NAMES.get(row["target_type"], "其他")
                event = f"#{row['event_id']} · {row['event_title']}" if row["event_id"] is not None else "—"
                table.insert("", "end", values=(
                    row["created_at"].replace("T", " "), row["action"],
                    row["name"],
                    f"{kind} #{row['target_id']}", event, row["reason"],
                ))

        ttk.Button(
            filters, text="查询",
            command=lambda: self.run(search, parent=window),
        ).pack(side="left")
        ttk.Label(panel, text="日期格式为 年-月-日。最多显示最近 500 条。").pack(
            anchor="w", pady=(6, 0),
        )
        search()
        window.wait_visibility()
        window.grab_set()

    def show_session_report(self):
        session = self.selected()["session_id"]
        actor = self.actor["id"]
        details, statistics, roster, finished = self.service.session_report(
            actor, session,
        )
        window = tk.Toplevel(self.root)
        window.title("场次统计与导出")
        window.transient(self.root)
        panel = ttk.Frame(window, padding=16)
        panel.pack(fill="both", expand=True)
        heading = ttk.Label(
            panel, text=f"{details['title']} · {self.session_label(details)}",
            style="ListTitle.TLabel",
        )
        heading.pack(anchor="w", pady=(0, 6))
        summary = tk.StringVar(window)
        ttk.Label(panel, textvariable=summary).pack(anchor="w", pady=(0, 10))
        headings = ("票种", "票种状态", "容量", "已用", "预留", "余票", "已签到", "签到率", "缺席", "缺席率")
        table = ttk.Treeview(
            panel, columns=headings, show="headings", height=8,
        )
        for name in headings:
            table.heading(name, text=name)
            table.column(
                name, width=105 if name == "票种" else 68, anchor="center",
            )
        table.pack(fill="both", expand=True)

        def render(session_info, rows, tickets, ended):
            checked = sum(row["checked"] for row in rows)
            absence_state = "场次已结束" if ended else "场次未结束或未设置结束时间"
            if session_info["cancelled_at"]:
                absence_state = "场次已取消"
            summary.set(f"有效票 {len(tickets)} 张，已签到 {checked} 张 · {absence_state}")
            table.delete(*table.get_children())
            for row in rows:
                count = row["issued"]
                absent = row["absent"]
                checked_rate = f"{row['checked'] / count:.1%}" if count else "—"
                absent_rate = f"{absent / count:.1%}" if count and absent is not None else "—"
                table.insert("", "end", values=(
                    row["name"], "已关闭" if row["closed"] else "—",
                    row["capacity"],
                    count, row["reserved"], row["available"],
                    row["checked"], checked_rate,
                    absent if absent is not None else "—", absent_rate,
                ))

        def refresh():
            render(*self.service.session_report(actor, session))

        def export():
            path = filedialog.asksaveasfilename(
                parent=window, title="保存报名名单", defaultextension=".csv",
                filetypes=[("CSV 文件", "*.csv")],
                initialfile=f"场次{session}_报名名单.csv",
            )
            if path:
                count = self.service.export_roster(actor, session, path)
                self.show_notice(
                    "导出完成", f"已导出 {count} 张有效票的记录。", parent=window,
                )

        render(details, statistics, roster, finished)
        buttons = ttk.Frame(panel)
        buttons.pack(anchor="e", pady=(10, 0))
        ttk.Button(
            buttons, text="刷新",
            command=lambda: self.run(refresh, parent=window),
        ).pack(
            side="left", padx=(0, 8),
        )
        ttk.Button(buttons, text="导出报名名单", style="Primary.TButton",
                   command=lambda: self.run(
                       export, parent=window,
                   )).pack(side="left")
        window.wait_visibility()
        window.grab_set()

    def confirm_action(self, title, message, *, parent=None, label="确认"):
        return self.form(
            title, (), parent=parent, message=message, confirm_label=label,
        ) is not None

    def show_notice(self, title, message, *, parent=None):
        self.form(
            title, (), parent=parent, message=message, confirm_label="关闭",
            show_cancel=False,
        )

    def form(
        self, title, fields, *, parent=None, multiline_last=False, message="",
        multiline_hint="",
        on_submit=None, confirm_label="确认", show_cancel=True,
    ):
        parent = parent or self.root
        dialog = tk.Toplevel(parent)
        dialog.withdraw()
        dialog.title(title)
        dialog.transient(parent)
        dialog.resizable(False, False)
        panel = ttk.Frame(dialog, style="Surface.TFrame", padding=20)
        panel.pack(fill="both", expand=True)
        if message:
            ttk.Label(
                panel, text=message, wraplength=500, style="Surface.TLabel",
            ).pack(
                anchor="w", pady=(0, 12),
            )
        widgets = []
        for index, (label, default) in enumerate(fields):
            ttk.Label(
                panel, text=label, style="Surface.TLabel",
            ).pack(anchor="w", pady=(0, 3))
            multiline = multiline_last and index == len(fields) - 1
            if multiline:
                if multiline_hint:
                    ttk.Label(
                        panel, text=multiline_hint, style="Surface.TLabel",
                    ).pack(
                        anchor="w", pady=(0, 4),
                    )
                widget = tk.Text(
                    panel, width=50, height=4, font=("Microsoft YaHei UI", 10),
                                 foreground="#10284b", padx=8, pady=5,
                                 relief="flat", highlightthickness=1,
                                 highlightbackground="#cbd9ee",
                                 highlightcolor="#155dcc")
                widget.insert("1.0", default)
            elif isinstance(default, tuple):
                widget = ttk.Combobox(
                    panel, state="readonly", values=default,
                    style="Choice.TCombobox",
                )
                widget.current(0)
            else:
                widget = ttk.Entry(panel, width=50, style="Form.TEntry")
                widget.insert(0, default)
            widget.pack(fill="x", pady=(0, 10))
            widgets.append((widget, multiline))
        result = None

        def confirm():
            nonlocal result
            values = [
                widget.get("1.0", "end-1c") if multiline else widget.get()
                for widget, multiline in widgets
            ]
            if on_submit is not None:
                try:
                    on_submit(values)
                except OverflowError:
                    self.show_notice("无法保存", "数值超出可用范围，请检查输入。", parent=dialog)
                    return
                except (ValueError, InvalidOperation, OSError, sqlite3.Error) as exc:
                    self.show_notice("无法保存", str(exc), parent=dialog)
                    return
            result = values
            dialog.destroy()

        buttons = ttk.Frame(panel, style="Surface.TFrame")
        buttons.pack(anchor="e")
        accept = ttk.Button(
            buttons, text=confirm_label, style="Primary.TButton",
            command=confirm,
        )
        focus = accept
        if show_cancel:
            cancel = ttk.Button(
                buttons, text="取消", style="Action.TButton",
                command=dialog.destroy,
            )
            cancel.pack(side="left", padx=(0, 8))
            cancel.bind("<Return>", lambda _: cancel.invoke())
            focus = cancel
        accept.pack(side="left")
        accept.bind("<Return>", lambda _: accept.invoke())
        for widget, _ in widgets:
            if isinstance(
                widget, ttk.Entry,
            ) and not isinstance(widget, ttk.Combobox):
                widget.bind("<Return>", lambda _: accept.invoke())
        self.wait_for_modal(
            dialog, parent, widgets[0][0] if widgets else focus,
        )
        return result

    def wait_for_modal(self, dialog, parent, focus):
        previous_grab = parent.grab_current()
        previous_focus = parent.focus_get()
        dialog.bind("<Escape>", lambda _: dialog.destroy())
        dialog.tk.call("::tk::PlaceWindow", dialog._w, "widget", parent._w)
        dialog.wait_visibility()
        dialog.grab_set()
        focus.focus_set()
        parent.wait_window(dialog)
        if previous_grab is not None and previous_grab.winfo_exists():
            previous_grab.grab_set()
        if previous_focus is not None and previous_focus.winfo_exists():
            previous_focus.focus_set()


def main():
    parser = ArgumentParser(description="活动票务与报名系统桌面原型")
    parser.add_argument(
        "--db", type=Path, default=Path(__file__).with_name("lab3.sqlite3"),
    )
    args = parser.parse_args()
    root = tk.Tk()
    service = Ticketing(args.db)
    service.seed_demo_users()
    Desktop(root, service)
    root.mainloop()


if __name__ == "__main__":
    main()
