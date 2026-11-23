"""Tkinter entry point for the activity ticketing prototype."""

from argparse import ArgumentParser
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sqlite3
import tkinter as tk
from tkinter import filedialog, font as tkfont, ttk

from ticketing import (
    BOOKING_CUTOFF, SELF_CANCELLATION_CUTOFF, AlreadyCheckedIn, RuleError,
    SessionDraft, SoldOut, TicketRequest, Ticketing,
)

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
ROLE_WORKSPACES = {
    "organizer": "组织者工作台", "admin": "平台管理后台",
    "participant": "参与者", "checker": "检票工作台",
}
ADMIN_PAGES = ("活动监管", "审核管理", "操作日志")
LOG_TARGET_NAMES = {
    "event": "活动", "session": "场次", "ticket_type": "票种", "registration": "报名",
    "waitlist": "候补", "ticket": "电子票",
}


class CardList(ttk.Frame):
    """Lay out cards from the top and scroll only overflowing content."""

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
        """Defer repeated layout changes to a single scrollbar update."""
        if self.scrollbar_update is None:
            self.scrollbar_update = self.after_idle(self.update_scrollbar)

    def update_scrollbar(self):
        """Clamp the offset and show the scrollbar when content overflows."""
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
        """Translate the Tk scrollbar command into a pixel offset."""
        if mode == "moveto":
            self.offset = int(float(value) * self.body.winfo_reqheight())
        else:
            step = self.viewport.winfo_height() if unit == "pages" else 36
            self.offset += int(value) * step
        self.update_scrollbar()

    def scroll(self, delta):
        """Handle a mouse-wheel step when the scrollbar is visible."""
        if not delta or not self.scrollbar.winfo_ismapped():
            return False
        self.move_scrollbar("scroll", -1 if delta > 0 else 1, "units")
        return True

    def clear(self):
        """Remove all cards and reset the scroll position for new content."""
        for widget in self.body.winfo_children():
            widget.destroy()
        self.offset = 0
        self.schedule_scrollbar_update()


class Desktop:
    """Display the workflow for the current demonstration identity."""

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
        self.admin_page = ADMIN_PAGES[0]
        self.log_page = None
        root.title("活动票务与报名系统")
        root.geometry("1220x740")
        root.minsize(940, 610)
        self.configure_styles()

        header = ttk.Frame(root, style="Header.TFrame", padding=(24, 16))
        header.pack(fill="x")
        calendar = tk.Canvas(
            header, width=30, height=30, highlightthickness=0,
            background=ttk.Style(root).lookup("Header.TFrame", "background"),
        )
        blue = "#1267ed"
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
        ttk.Separator(header, orient="vertical").pack(
            side="left", fill="y", padx=16, pady=5,
        )
        self.workspace_name = ttk.Label(header, style="Header.TLabel")
        self.workspace_name.pack(side="left")
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

        body = ttk.Frame(root, style="Workspace.TFrame")
        body.pack(fill="both", expand=True)
        self.sidebar = ttk.Frame(body, style="Side.TFrame", padding=(12, 18))
        self.sidebar.pack(side="left", fill="y")
        panel = ttk.Frame(body, style="Workspace.TFrame", padding=(20, 16))
        panel.pack(fill="both", expand=True)
        self.page_heading = ttk.Frame(panel, style="Workspace.TFrame")
        self.page_heading.pack(fill="x", pady=(0, 12))
        self.create_button = ttk.Button(
            self.page_heading, text="+ 创建活动", style="Primary.TButton",
            command=lambda: self.run(self.create_event),
        )
        self.role_heading = ttk.Label(
            self.page_heading, style="Section.TLabel",
        )
        self.role_heading.pack(anchor="w")
        self.page_description = ttk.Label(
            self.page_heading, style="Status.TLabel",
        )
        self.page_description.pack(anchor="w", pady=(3, 0))
        self.stats = ttk.Frame(panel, style="Workspace.TFrame")
        self.stats.bind("<Configure>", self.layout_stats)
        self.actions = ttk.Frame(panel, style="Workspace.TFrame")
        self.actions.bind("<Configure>", self.layout_actions)

        self.listing = ttk.Frame(panel, style="Surface.TFrame", padding=14)
        self.logs_container = ttk.Frame(panel, padding=14)
        list_header = ttk.Frame(self.listing, style="Surface.TFrame")
        list_header.pack(fill="x", pady=(0, 12))
        self.list_heading = ttk.Label(list_header, style="ListTitle.TLabel")
        self.list_heading.pack(side="left")
        self.list_count = ttk.Label(list_header, style="Surface.TLabel")
        self.list_count.pack(side="left", padx=(10, 0))
        controls = ttk.Frame(self.listing)
        controls.pack(fill="x", pady=(0, 10))
        ttk.Label(controls, text="活动名称").pack(side="left")
        self.search = ttk.Entry(controls, width=19, style="Form.TEntry")
        self.search.pack(side="left", padx=(8, 8))
        self.search.bind("<Return>", lambda _: self.run(self.refresh))
        self.filter_bar = ttk.Frame(controls)
        self.status_filter = ttk.Combobox(
            self.filter_bar, width=12, state="readonly",
            style="Choice.TCombobox",
        )
        self.status_filter.pack(side="left", padx=(0, 8))
        self.status_filter.bind(
            "<<ComboboxSelected>>", lambda _: self.run(self.refresh),
        )
        query = ttk.Frame(controls)
        query.pack(side="left")
        ttk.Button(
            query, text="查询", style="Primary.TButton",
            command=lambda: self.run(self.refresh),
        ).pack(side="left")
        ttk.Button(
            query, text="重置", command=self.reset_dashboard_search,
        ).pack(side="left", padx=(8, 0))
        self.query_bar = query

        lists = ttk.Frame(self.listing)
        lists.pack(fill="both", expand=True)
        self.activity_panel = ttk.Frame(lists)
        activity_table_frame = ttk.Frame(self.activity_panel)
        activity_table_frame.pack(fill="both", expand=True)
        activity_columns = (
            ("title", "活动名称", 170, ()),
            ("time", "最近场次", 148, ("0000-00-00 00:00",)),
            ("count", "场次数", 66, ()),
            ("status", "活动状态", 96, (*STATES.values(), "变更待审核")),
        )
        self.activity_table = ttk.Treeview(
            activity_table_frame,
            columns=[column for column, *_ in activity_columns],
            show="headings", selectmode="browse", height=4,
        )
        style = ttk.Style(root)
        row_font = tkfont.Font(
            root=root, font=style.lookup("Treeview", "font"),
        )
        heading_font = tkfont.Font(
            root=root, font=style.lookup("Treeview.Heading", "font"),
        )
        for column, label, width, examples in activity_columns:
            minimum = max(
                heading_font.measure(label),
                max(map(row_font.measure, examples), default=0),
            ) + 20
            self.activity_table.heading(column, text=label, anchor="w")
            self.activity_table.column(
                column, width=max(width, minimum), minwidth=minimum,
                stretch=column in ("title", "status"),
            )
        activity_scroll = ttk.Scrollbar(
            activity_table_frame, orient="vertical",
            command=self.activity_table.yview,
        )
        activity_scroll.pack(side="right", fill="y")
        self.activity_table.pack(side="left", fill="both", expand=True)
        horizontal_scroll = ttk.Scrollbar(
            activity_table_frame, orient="horizontal",
            command=self.activity_table.xview,
        )

        def update_horizontal_scroll(first, last):
            horizontal_scroll.set(first, last)
            if float(first) == 0 and float(last) == 1:
                horizontal_scroll.pack_forget()
            elif not horizontal_scroll.winfo_manager():
                horizontal_scroll.pack(
                    side="bottom", fill="x", before=self.activity_table,
                )

        self.activity_table.configure(
            yscrollcommand=activity_scroll.set,
            xscrollcommand=update_horizontal_scroll,
        )
        self.activity_table.tag_configure("alternate", background="#f4f8fe")
        self.activity_table.bind(
            "<<TreeviewSelect>>", self.show_activity_sessions,
        )

        self.detail_divider = ttk.Separator(lists, orient="vertical")
        self.session_panel = ttk.Frame(lists)
        self.session_bar = ttk.Frame(self.session_panel)
        self.session_heading = ttk.Label(
            self.session_bar, style="ListTitle.TLabel",
        )
        self.session_heading.pack(side="left", fill="x", expand=True)
        self.activity_detail = ttk.Label(
            self.session_panel, style="CardDetail.TLabel", padding=(10, 8),
            wraplength=240, justify="left",
        )
        self.detail_actions = ttk.Frame(self.session_panel)
        self.session_actions = ttk.Frame(self.session_panel)
        self.cancel_session_button = ttk.Button(
            self.session_actions, text="取消场次",
            command=lambda: self.run(
                lambda: self.submit_closure(close_ticket=False),
            ),
        )
        self.cancel_session_button.pack(side="left")
        self.close_ticket_button = ttk.Button(
            self.session_actions, text="关闭票种",
            command=lambda: self.run(
                lambda: self.submit_closure(close_ticket=True),
            ),
        )
        self.close_ticket_button.pack(side="left", padx=(6, 0))
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
        columns = ("title", "id", "status", "venue", "start")
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
        self.table.bind("<<TreeviewSelect>>", self.session_selection_changed)
        column_layout = (
            ("title", "活动", 220, 125),
            ("id", "场次编号", 66, 66),
            ("status", "场次状态", 89, 77),
            ("venue", "地点", 92, 65),
            ("start", "开始时间", 126, 115),
        )
        for column, label, width, minimum in column_layout:
            minimum = max(minimum, heading_font.measure(label) + 20)
            self.table.heading(column, text=label, anchor="w")
            self.table.column(
                column, width=max(width, minimum), minwidth=minimum,
                stretch=column != "id" and width > minimum,
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
        self.status_label = ttk.Label(
            panel, textvariable=self.status, style="Status.TLabel",
        )
        self.status_label.pack(
            side="bottom", fill="x", pady=(8, 0), before=self.page_heading,
        )
        self.switch_account()
        self.root.after(15000, self.tick_deadlines)

    def build_checker_panel(self, parent):
        """Build the ticket input and the separate check-in confirmation."""
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
        """Set the shared Tkinter palette, fonts, and control styles."""
        style = ttk.Style(self.root)
        style.theme_use("clam")
        font = "Microsoft YaHei UI"
        workspace = "#edf3ff"
        navy = "#112344"
        blue = "#0966ed"
        border = "#d6e2f4"
        header = "#e8f0fc"
        sidebar = "#f7faff"
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
            "Treeview", rowheight=34, font=(font, 10), foreground=navy,
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
            "Treeview", background=[("selected", "#e1edff")],
            foreground=[("selected", navy)],
        )
        style.configure(
            "Vertical.TScrollbar", background="#e6eef9", troughcolor="#f4f8fe",
                        bordercolor=border, arrowcolor=navy)
        style.configure("Header.TFrame", background=header)
        style.configure(
            "HeaderTitle.TLabel", background=header, foreground=navy,
            font=(font, 20, "bold"),
        )
        style.configure(
            "Header.TLabel", background=header, foreground=navy,
            font=(font, 11),
        )
        style.configure("Choice.TCombobox", padding=(8, 5), arrowsize=14)
        style.map("Choice.TCombobox", fieldbackground=[("readonly", "white")],
                  foreground=[("readonly", navy)],
                  selectbackground=[("readonly", "white")],
                  selectforeground=[("readonly", navy)])
        style.configure("Workspace.TFrame", background=workspace)
        style.configure("Side.TFrame", background=sidebar)
        style.configure(
            "Nav.TButton", background=sidebar, bordercolor=sidebar,
            anchor="w", padding=(11, 10), foreground=navy,
        )
        style.configure(
            "Selected.Nav.TButton", background="#dfeaff",
            bordercolor="#dfeaff", foreground=blue, font=(font, 10, "bold"),
        )
        style.map(
            "Selected.Nav.TButton",
            background=[("active", "#d3e4ff"), ("pressed", "#c7deff")],
        )
        style.configure(
            "Metric.TFrame", background="white", bordercolor=border,
            relief="solid", borderwidth=1,
        )
        style.configure(
            "MetricValue.TLabel", background="white", foreground=navy,
            font=(font, 18, "bold"),
        )
        style.configure(
            "Danger.TButton", background="white", foreground="#a92236",
            bordercolor="#eaa9b2", font=(font, 10, "bold"),
        )
        style.configure(
            "Section.TLabel", background=workspace, foreground=navy,
            font=(font, 20, "bold"),
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
        """Process waitlist deadlines on the desktop application's timer."""
        try:
            self.service.process_waitlists()
        except sqlite3.Error as exc:
            self.status.set(f"状态更新失败，稍后重试：{exc}")
        finally:
            self.root.after(15000, self.tick_deadlines)

    def run(self, action, *, parent=None):
        """Show recoverable action errors in the relevant window."""
        try:
            action()
        except (ValueError, InvalidOperation, OSError, sqlite3.Error) as exc:
            self.show_notice("操作失败", str(exc), parent=parent)

    def switch_account(self, _event=None):
        """Select a demo identity and reset filters from the previous role."""
        self.actor = self.users[self.account.current()]
        role = self.actor["role"]
        self.workspace_name.configure(text=ROLE_WORKSPACES[role])
        self.admin_page = ADMIN_PAGES[0]
        self.filters.clear()
        self.search.delete(0, "end")
        self.show_workspace()

    def show_workspace(self):
        """Rebuild navigation and controls for the selected role and page."""
        role = self.actor["role"]
        page = self.admin_page if role == "admin" else ROLE_HEADINGS[role]
        logging = role == "admin" and page == "操作日志"
        self.role_heading.configure(text=page)
        descriptions = {
            "活动监管": "查看活动和场次，处理需停用或取消的活动。",
            "审核管理": "处理活动发布、变更、场次取消和票种关闭申请。",
            "操作日志": "查询操作时间、操作人、关联活动及处理原因。",
            "活动管理": "创建并维护活动，跟踪审核进度和各场次安排。",
            "活动浏览与报名": "选择活动和场次，查看余票并报名。",
            "场次检票": "选择获授权的场次，核验电子票后确认签到。",
        }
        self.page_description.configure(text=descriptions[page])
        self.create_button.pack_forget()
        for panel in (self.sidebar, self.actions, self.detail_actions):
            for widget in panel.winfo_children():
                widget.destroy()
        if self.log_page is not None:
            self.log_page.destroy()
            self.log_page = None

        organizer_nav = (
            ("活动管理", self.reset_dashboard_search),
            ("创建活动", self.create_event),
            ("报名管理", self.show_payments),
            ("数据统计", self.show_session_report),
        )
        participant_nav = (
            ("活动首页", self.reset_dashboard_search),
            ("我的电子票", self.show_tickets),
            ("我的候补", self.show_waitlist),
            ("我的通知", self.show_notices),
        )
        navigation = {
            "organizer": organizer_nav,
            "admin": tuple((name, lambda item=name: self.open_admin_page(item))
                           for name in ADMIN_PAGES),
            "participant": participant_nav,
            "checker": (("场次检票", self.refresh),),
        }
        for label, action in navigation[role]:
            selected = label == page or (
                role == "participant" and label == "活动首页"
            )
            ttk.Button(
                self.sidebar, text=label, width=12,
                style="Selected.Nav.TButton" if selected else "Nav.TButton",
                command=lambda callback=action: self.run(callback),
            ).pack(fill="x", pady=(0, 6))

        if logging:
            self.listing.pack_forget()
            self.stats.pack_forget()
            self.actions.pack_forget()
            self.logs_container.pack(fill="both", expand=True)
            self.show_operation_logs()
            return
        self.logs_container.pack_forget()
        self.listing.pack(fill="both", expand=True)
        self.layout_role_lists(role)
        actions = {
            "organizer": [
                ("提交审核", self.submit_review),
                ("提交变更", self.submit_change),
                ("授权检票员", self.assign_checker),
            ],
            "admin": [],
            "participant": [("筛选场次", self.filter_sessions)],
            "checker": [],
        }
        if actions[role]:
            self.actions.pack(fill="x", pady=(0, 8), before=self.listing)
        else:
            self.actions.pack_forget()
        if role in ("organizer", "admin"):
            self.stats.pack(
                fill="x", pady=(0, 12),
                before=self.actions if actions[role] else self.listing,
            )
        else:
            self.stats.pack_forget()
        for label, action in actions[role]:
            ttk.Button(
                self.actions, text=label, width=-12,
                style="Action.TButton",
                command=lambda callback=action: self.run(callback),
            )
        if role == "organizer":
            self.create_button.pack(
                side="right", padx=(12, 0), before=self.role_heading,
            )
            ttk.Button(
                self.detail_actions, text="下架或取消活动", style="Danger.TButton",
                command=lambda: self.run(self.manage_event),
            ).pack(side="left")
        elif role == "admin":
            self.add_admin_actions()
        self.layout_actions()
        self.run(self.refresh)

    def open_admin_page(self, page):
        """Switch admin pages and clear the previous activity search."""
        self.admin_page = page
        self.search.delete(0, "end")
        self.show_workspace()

    def add_admin_actions(self):
        """Install moderation controls for the selected admin page."""
        if self.admin_page == "审核管理":
            actions = (
                ("通过审核", "Primary.TButton", lambda: self.review(True)),
                ("驳回审核", "Action.TButton", lambda: self.review(False)),
            )
        else:
            actions = (
                ("停用活动", "Danger.TButton", self.disable_event),
                ("管理活动", "Action.TButton", self.manage_event),
                ("代取消报名", "Action.TButton", self.assisted_cancel),
            )
        for label, style, action in actions:
            ttk.Button(
                self.detail_actions, text=label, style=style,
                command=lambda callback=action: self.run(callback),
            ).pack(side="left", padx=(0, 5))

    def reset_dashboard_search(self):
        """Clear the keyword and role-specific filters, then reload."""
        self.search.delete(0, "end")
        self.filters.clear()
        if self.status_filter["values"]:
            self.status_filter.current(0)
        self.run(self.refresh)

    def layout_role_lists(self, role):
        """Place the activity, session, or checker panels for this role."""
        for frame in (
            self.activity_panel, self.session_panel, self.session_bar,
            self.participant_browser, self.checker_panel, self.detail_divider,
            self.activity_detail, self.detail_actions, self.session_actions,
            self.filter_bar,
        ):
            frame.pack_forget()
        for button in (self.add_session_button, self.edit_session_button):
            button.pack_forget()
        self.list_count.configure(text="")

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
            self.table.master.pack_configure(fill="both", expand=True)
            self.session_panel.pack(side="left", fill="both", padx=(0, 18))
            self.checker_panel.pack(side="left", fill="both", expand=True)
            return

        reviewing = role == "admin" and self.admin_page == "审核管理"
        self.list_heading.configure(text="待审核申请" if reviewing else "活动列表")
        options = ("全部申请", "发布审核", "变更审核") if reviewing else (
            "全部状态", *STATES.values(), "变更待审核",
        )
        self.status_filter.configure(values=options)
        self.status_filter.current(0)
        self.filter_bar.pack(
            side="left", before=self.query_bar,
        )
        self.table.configure(
            displaycolumns=("id", "start", "venue", "status"), height=4,
        )
        self.table.master.pack_configure(fill="x", expand=False)
        self.session_panel.pack(side="right", fill="y")
        self.detail_divider.pack(side="right", fill="y", padx=12)
        self.activity_panel.pack(side="left", fill="both", expand=True)
        self.session_bar.pack(fill="x", before=self.table.master, pady=(0, 8))
        self.activity_detail.pack(
            fill="x", before=self.table.master, pady=(0, 8),
        )
        if role == "admin":
            self.detail_actions.pack(
                fill="x", pady=(10, 0),
            )

    def scroll_participant_cards(self, event):
        """Consume the mouse wheel only over a scrollable card list."""
        if self.actor["role"] != "participant":
            return None
        for view in (self.participant_activities, self.participant_sessions):
            if (
                str(event.widget).startswith(f"{view}.")
                and view.scroll(event.delta)
            ):
                return "break"
        return None

    def layout_actions(self, event=None):
        """Wrap action buttons into rows based on the available width."""
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
        """Reload visible records using the filters available to the role."""
        role = self.actor["role"]
        if role == "admin" and self.admin_page == "操作日志":
            return
        keyword = self.search.get().strip()
        if role in ("organizer", "admin"):
            sessions = self.service.list_sessions(self.actor["id"])
            self.refresh_dashboard(sessions, keyword)
            return
        sessions = self.service.list_sessions(
            self.actor["id"], keyword=keyword,
            **self.filters,
        )
        if role == "participant":
            self.refresh_activities(self.group_sessions_by_activity(sessions))
        else:
            self.render_sessions(sessions)

    def refresh_dashboard(self, sessions, keyword):
        """Count distinct activities before applying the visible filters."""
        reviewing = (
            self.actor["role"] == "admin" and self.admin_page == "审核管理"
        )
        selected = self.status_filter.get()
        selected = {
            "全部状态": "", "全部申请": "",
            "发布审核": "待审核", "变更审核": "变更待审核",
        }.get(selected, selected)

        statuses = Counter()
        visible = {}
        activities = self.group_sessions_by_activity(sessions)
        for event_id, rows in activities.items():
            activity = rows[0]
            state = self.activity_status(activity)
            statuses[state] += 1
            if (
                keyword in activity["title"]
                and (not reviewing or activity["status"] == "pending")
                and (not selected or state == selected)
            ):
                visible[event_id] = rows

        statuses["活动总数"] = len(activities)
        statuses["待审核申请"] = statuses["待审核"] + statuses["变更待审核"]
        if reviewing:
            labels = ("待审核申请", "发布审核", "变更审核")
            statuses["发布审核"] = statuses["待审核"]
            statuses["变更审核"] = statuses["变更待审核"]
        elif self.actor["role"] == "admin":
            labels = ("活动总数", "已发布", "待审核申请", "已下架", "已停用")
        else:
            labels = (
                "活动总数", "已发布", "待审核申请", "草稿", "已下架",
                "已停用", "已取消",
            )
        self.show_stats(labels, statuses)
        self.refresh_activities(visible)
        unit = "项申请" if reviewing else "项活动"
        total = f"共 {len(self.activities)} {unit}"
        self.list_count.configure(text=total)
        self.status.set(total)

    def show_stats(self, labels, statuses):
        """Render the counts and status icons for the active dashboard."""
        for widget in self.stats.winfo_children():
            widget.destroy()
        icons = {
            "活动总数": ("calendar", "#1769e8", "#e7f1ff"),
            "已发布": ("check", "#14804a", "#e6f7ec"),
            "待审核申请": ("clock", "#a86507", "#fff3db"),
            "草稿": ("pencil", "#167fab", "#e5f5fc"),
            "已下架": ("archive", "#5e6c85", "#eef1f6"),
            "已停用": ("disabled", "#bd3046", "#fde9ed"),
            "已取消": ("cancel", "#bd3046", "#fde9ed"),
            "发布审核": ("calendar", "#1769e8", "#e7f1ff"),
            "变更审核": ("change", "#7357bc", "#f0eafb"),
        }
        for label in labels:
            card = ttk.Frame(
                self.stats, style="Metric.TFrame", padding=(11, 7),
            )
            icon, color, tint = icons[label]
            marker = tk.Canvas(
                card, width=36, height=36, background="white",
                highlightthickness=0,
            )
            self.draw_metric_icon(marker, icon, color, tint)
            marker.pack(side="left", padx=(0, 9))
            numbers = ttk.Frame(card)
            numbers.pack(side="left")
            tk.Label(
                numbers, text=label, background="white", foreground=color,
                font=("Microsoft YaHei UI", 10, "bold"),
            ).pack(anchor="w")
            ttk.Label(
                numbers, text=statuses[label], style="MetricValue.TLabel",
            ).pack(anchor="w")
        self.stats.after_idle(self.layout_stats)

    def layout_stats(self, event=None):
        """Fit the metric cards into rows at the available width."""
        cards = self.stats.winfo_children()
        if not cards:
            return
        available = event.width if event else self.stats.winfo_width()
        widest = max(card.winfo_reqwidth() for card in cards) + 8
        per_row = min(len(cards), max(1, available // widest))
        for index in range(len(STATES) + 1):
            self.stats.columnconfigure(index, weight=int(index < per_row))
        for index, card in enumerate(cards):
            row, column = divmod(index, per_row)
            card.grid(
                row=row, column=column, sticky="nsew",
                padx=(0, 8 if column + 1 < per_row else 0), pady=(0, 4),
            )

    @staticmethod
    def draw_metric_icon(canvas, icon, color, tint):
        """Draw a dashboard icon on its tinted circular background."""
        canvas.create_oval(1, 1, 35, 35, fill=tint, outline="")

        def stroke(*points):
            canvas.create_line(
                *points, fill=color, width=2, capstyle="round",
                joinstyle="round",
            )

        if icon in ("check", "clock", "disabled", "cancel"):
            canvas.create_oval(9, 9, 27, 27, outline=color, width=2)
        match icon:
            case "calendar":
                canvas.create_rectangle(9, 11, 27, 26, outline=color, width=2)
                stroke(9, 16, 27, 16)
                stroke(14, 8, 14, 13)
                stroke(22, 8, 22, 13)
            case "check":
                stroke(13, 18, 17, 22, 23, 15)
            case "clock":
                stroke(18, 12, 18, 18, 23, 21)
            case "pencil":
                stroke(10, 26, 12, 20, 22, 10, 26, 14, 16, 24, 10, 26)
                stroke(19, 13, 23, 17)
            case "archive":
                canvas.create_rectangle(9, 10, 27, 26, outline=color, width=2)
                stroke(18, 14, 18, 22)
                stroke(14, 18, 18, 22, 22, 18)
            case "disabled":
                stroke(12, 24, 24, 12)
            case "cancel":
                stroke(14, 14, 22, 22)
                stroke(22, 14, 14, 22)
            case "change":
                stroke(10, 14, 26, 14, 22, 10)
                stroke(26, 22, 10, 22, 14, 26)

    @staticmethod
    def activity_status(row):
        """Distinguish pending changes from an unpublished activity."""
        if row["status"] == "pending" and row["is_public"]:
            return "变更待审核"
        return STATES[row["status"]]

    @staticmethod
    def session_label(row):
        """Format a session's stored start time and venue for display."""
        return f"{row['starts_at'].replace('T', ' ')} · {row['venue']}"

    @staticmethod
    def group_sessions_by_activity(sessions):
        """Keep each activity's sessions together in display order."""
        activities = {}
        for session in sessions:
            activities.setdefault(str(session["event_id"]), []).append(session)
        return activities

    def refresh_activities(self, activities):
        """Update the activity list and restore its selected activity."""
        self.activities = activities
        if self.actor["role"] == "participant":
            self.render_activity_cards()
            return

        previous = self.activity_table.selection()
        self.activity_table.delete(*self.activity_table.get_children())
        now = self.service.clock()
        for index, (event_id, rows) in enumerate(self.activities.items()):
            activity = rows[0]
            recent = next(
                (row for row in rows
                 if datetime.fromisoformat(row["starts_at"]) >= now),
                rows[-1],
            )
            self.activity_table.insert(
                "", "end", iid=event_id,
                tags=("alternate",) if index % 2 else (),
                values=(
                    activity["title"],
                    recent["starts_at"].replace("T", " ")[:16],
                    len(rows), self.activity_status(activity),
                ),
            )
        if self.activities:
            event_id = (
                previous[0]
                if previous and previous[0] in self.activities
                else next(iter(self.activities))
            )
            self.activity_table.selection_set(event_id)
            self.activity_table.see(event_id)
        self.show_activity_sessions()

    def render_activity_cards(self):
        """Rebuild activity cards and preserve the selected activity."""
        self.participant_activities.clear()
        self.activity_cards.clear()
        if self.participant_event_id not in self.activities:
            self.participant_event_id = next(iter(self.activities), None)
        for event_id, rows in self.activities.items():
            card = ttk.Button(
                self.participant_activities.body,
                text=f"{rows[0]['title']}\n{len(rows)} 个场次",
                command=lambda key=event_id: self.run(
                    lambda: self.show_participant_activity(key),
                ),
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
        """Load live stock and show sessions for the selected activity."""
        rows = self.activities.get(event_id, [])
        stock = self.service.participant_session_stock(
            self.actor["id"], event_id,
        ) if rows else {}
        self.participant_event_id = event_id
        for key, card in self.activity_cards.items():
            style = (
                "SelectedActivityCard.TButton"
                if key == event_id else "ActivityCard.TButton"
            )
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
        """Show a session's availability and its booking entry point."""
        start = datetime.fromisoformat(row["starts_at"])
        period = start.strftime("%Y-%m-%d %H:%M")
        if row["ends_at"]:
            end = datetime.fromisoformat(row["ends_at"])
            end_text = end.strftime(
                "%H:%M" if end.date() == start.date() else "%Y-%m-%d %H:%M"
            )
            period = f"{period} – {end_text}"
        has_ticket_types = bool(row["open_ticket_types"])
        open_for_booking = has_ticket_types and now < start - BOOKING_CUTOFF
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
        """Show sessions and actions for the selected activity."""
        role = self.actor["role"]
        if role not in ("organizer", "admin"):
            return
        selection = self.activity_table.selection()
        rows = self.activities.get(selection[0], []) if selection else []
        reviewing = role == "admin" and self.admin_page == "审核管理"
        heading = "申请详情" if reviewing else "活动详情"
        detail = "选择一项活动查看详情。"
        if rows:
            activity = rows[0]
            detail = (
                f"{activity['title']}\n{self.activity_status(activity)}"
                f"  ·  {len(rows)} 个场次\n活动编号  #{activity['event_id']}"
            )
            if activity["review_reason"]:
                detail += f"\n说明  {activity['review_reason']}"
        self.session_heading.configure(text=heading)
        self.activity_detail.configure(text=detail)
        if role == "admin":
            for button in self.detail_actions.winfo_children():
                enabled = bool(rows)
                if button.cget("text") in ("停用活动", "管理活动"):
                    enabled = enabled and bool(rows[0]["is_public"])
                button.configure(state="normal" if enabled else "disabled")
        if rows is not self.sessions:
            self.render_sessions(rows)
        else:
            self.session_selection_changed()

    @staticmethod
    def session_status(row):
        """Display session cancellation before the parent activity state."""
        if row["cancelled_at"] or row["status"] == "cancelled":
            return "已取消"
        if row["is_public"]:
            return "正常"
        return STATES[row["status"]]

    def session_selection_changed(self, _event=None):
        """Update the controls that depend on the selected session."""
        role = self.actor["role"]
        if role == "checker":
            self.update_checker_session()
        elif role == "organizer":
            self.update_organizer_actions()

    def update_organizer_actions(self):
        """Match organizer controls to activity and session eligibility."""
        activity_selection = self.activity_table.selection()
        rows = (
            self.activities[activity_selection[0]]
            if activity_selection else []
        )
        detail_selection = self.table.selection()
        session = (
            self.sessions[int(detail_selection[0])]
            if detail_selection else None
        )
        activity = rows[0] if rows else None
        status = activity["status"] if activity else None
        active_session = session is not None and not session["cancelled_at"]
        published_session = status == "published" and active_session

        if status == "draft":
            self.add_session_button.configure(state="normal")
            self.add_session_button.pack(side="right")
            self.edit_session_button.configure(
                state="normal" if session is not None else "disabled",
            )
            self.edit_session_button.pack(side="right", padx=(0, 6))
        else:
            self.add_session_button.pack_forget()
            self.edit_session_button.pack_forget()

        if published_session:
            start = datetime.fromisoformat(session["starts_at"])
            future = start > self.service.clock()
            self.cancel_session_button.configure(
                state="normal" if future else "disabled",
            )
            self.close_ticket_button.configure(
                state="normal" if session["open_ticket_types"] else "disabled",
            )
            self.session_actions.pack(fill="x", pady=(8, 0))
        else:
            self.session_actions.pack_forget()
        if activity and activity["is_public"]:
            self.detail_actions.pack(fill="x", pady=(8, 0))
        else:
            self.detail_actions.pack_forget()

        enabled = {
            "提交审核": status in ("draft", "unlisted"),
            "提交变更": published_session,
            "授权检票员": active_session
            and status not in ("cancelled", "disabled"),
        }
        for button in self.actions.winfo_children():
            button.configure(
                state="normal" if enabled[button.cget("text")] else "disabled",
            )

    def render_sessions(self, sessions):
        """Keep the selected session by ID when rows are refreshed."""
        selection = self.table.selection()
        selected_session = (
            self.sessions[int(selection[0])]["session_id"]
            if selection else None
        )
        self.sessions = sessions
        self.table.delete(*self.table.get_children())
        selected_index = 0
        for index, row in enumerate(self.sessions):
            self.table.insert(
                "", "end", iid=str(index),
                tags=("alternate",) if index % 2 else (),
                values=(
                    row["title"], f"#{row['session_id']}",
                    self.session_status(row),
                    row["venue"],
                    row["starts_at"].replace("T", " ")[:16],
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
        if self.actor["role"] in ("organizer", "admin"):
            self.table.configure(height=max(1, min(len(self.sessions), 4)))
        self.status.set(f"共 {len(self.sessions)} 个场次")
        self.session_selection_changed()

    def filter_sessions(self):
        """Apply participant filters after validating the form values."""
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
        """Return the selected session or ask the user to select one."""
        selection = self.table.selection()
        if not selection:
            raise RuleError("请先选择一个场次。")
        return self.sessions[int(selection[0])]

    def selected_activity(self):
        """Return an activity row for the selected activity."""
        if self.actor["role"] not in ("organizer", "admin"):
            return self.selected()
        selection = self.activity_table.selection()
        if not selection:
            raise RuleError("请先选择一个活动。")
        return self.activities[selection[0]][0]

    def create_event(self):
        """Collect draft sessions before saving or submitting the activity."""
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
                minutes = int(
                    (draft.ends_at - draft.starts_at).total_seconds() // 60
                )
                table.insert("", "end", iid=str(index), values=(
                    draft.starts_at.strftime("%Y-%m-%d %H:%M"), draft.venue,
                    minutes,
                    draft.capacity, len(draft.ticket_types),
                ))
            if drafts:
                item = str(min(selected_index, len(drafts) - 1))
                table.selection_set(item)
                table.see(item)
            summary.set(
                f"共 {len(drafts)} 个场次，"
                f"总容量 {sum(draft.capacity for draft in drafts)} 人"
            )

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
        """Collect a new session for the selected activity draft."""
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
        """Load and edit the selected session in an activity draft."""
        row = self.selected()
        actor, session = self.actor["id"], row["session_id"]
        draft = self.service.draft_session(actor, session)
        if self.session_form(
            "编辑草稿场次", initial=draft, message=f"活动：{row['title']}",
            on_submit=lambda values: self.service.edit_draft_session(
                actor, session, values,
            ),
        ) is not None:
            self.refresh()
            self.status.set(f"已保存「{row['title']}」的场次修改")

    def session_form(
        self, title, *, parent=None, initial=None, message="", on_submit=None,
    ):
        """Collect one session and pass the parsed draft to the caller."""
        if initial is None:
            initial = SessionDraft(
                "", datetime.now() + timedelta(days=2), 30,
                (("普通票", 0, 20), ("VIP票", 2000, 10)),
            )
        minutes = (
            int((initial.ends_at - initial.starts_at).total_seconds() // 60)
            if initial.ends_at else 120
        )
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
        """Parse a count and identify the invalid field in any error."""
        try:
            value = int(raw)
        except ValueError as exc:
            raise RuleError(f"{label}必须是大于 0 的整数。") from exc
        if value < 1:
            raise RuleError(f"{label}必须是大于 0 的整数。")
        return value

    def parse_session(self, fields):
        """Validate schedule, capacity, and ticket quotas in the form."""
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
        """Request publication for the selected draft or unlisted event."""
        self.service.submit_review(
            self.actor["id"], self.selected_activity()["event_id"],
        )
        self.refresh()

    def manage_event(self):
        """Confirm an activity-wide action and record the operator's reason."""
        event = self.selected_activity()
        actor = self.actor
        option = "下架" if actor["role"] == "organizer" else "停用"
        if not event["is_public"]:
            raise RuleError("请选择一个仍对外开放的活动。")
        fields = self.form(
            f"{option}或取消活动",
            [("操作", (option, "取消")), ("操作原因", "")],
            message=f"活动：{event['title']}",
        )
        if fields is None:
            return
        action, reason = fields
        actions = {
            option: "unlisted" if option == "下架" else "disabled",
            "取消": "cancelled",
        }
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

    def disable_event(self):
        """Require a reason before disabling the administrator's selection."""
        event = self.selected_activity()
        fields = self.form(
            "停用活动", [("停用原因", "")],
            message=f"「{event['title']}」将停止展示和新报名，已有报名保留。",
            confirm_label="确认停用",
        )
        if fields is None:
            return
        self.service.set_event_status(
            self.actor["id"], event["event_id"], "disabled", fields[0],
        )
        self.refresh()
        self.status.set(f"已停用「{event['title']}」")

    @staticmethod
    def parse_price(price):
        """Convert a decimal price to whole cents without rounding."""
        cents = Decimal(price.strip()) * 100
        if not cents.is_finite() or cents != cents.to_integral_value():
            raise RuleError("请填写有效票价，最多保留两位小数。")
        return int(cents)

    def submit_change(self):
        """Collect changes for the selected session and one ticket type."""
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
        """Confirm the selected request and let the service apply it."""
        event_id = self.selected_activity()["event_id"]
        actor = self.actor["id"]
        request = self.service.pending_review(actor, event_id)
        summary = self.review_summary(request)
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

    @staticmethod
    def review_summary(request):
        """Describe changes that will take effect upon approval."""
        if request["closure_session"] is not None:
            location = (
                f"{request['closure_start'].replace('T', ' ')}"
                f" · {request['closure_venue']}"
            )
            if request["closure_ticket"] is None:
                return (
                    f"取消场次：{location}\n"
                    "该场次未签到的报名和候补将取消，其他场次照常。"
                )
            return (
                f"关闭票种：{request['closure_name']}\n"
                f"场次：{location}\n该票种的相关候补将取消。"
            )
        if request["session_id"] is not None:
            old_start = request["old_start"].replace("T", " ")
            start = request["starts_at"].replace("T", " ")
            return (
                f"开始时间：{old_start} → {start}\n"
                f"场次容量：{request['old_capacity']} "
                f"→ {request['session_capacity']}\n"
                f"{request['name']}容量：{request['old_quota']} "
                f"→ {request['ticket_capacity']}\n"
                f"票价：¥{request['old_price'] / 100:.2f} "
                f"→ ¥{request['price_cents'] / 100:.2f}"
            )
        return ""

    def submit_closure(self, *, close_ticket):
        """Request approval to cancel a session or close one ticket type."""
        selected = self.selected()
        actor, session = self.actor["id"], selected["session_id"]
        message = (
            f"{selected['title']} · 场次 #{session}\n"
            f"{self.session_label(selected)}\n管理员审核通过后生效。"
        )
        if close_ticket:
            tickets = self.service.closure_options(actor, session)
            choices = {row["name"]: row["id"] for row in tickets}
            if not choices:
                raise RuleError("该场次没有可申请关闭的票种。")
            fields = self.form(
                "申请关闭票种", [("票种", tuple(choices))],
                confirm_label="提交审核", message=message,
                on_submit=lambda values: self.service.submit_closure(
                    actor, session, ticket_type=choices[values[0]],
                ),
            )
            if fields is None:
                return
        else:
            if not self.confirm_action(
                "申请取消场次", message, label="提交审核",
            ):
                return
            self.service.submit_closure(actor, session)
        self.refresh()
        self.status.set("申请已提交，管理员审核通过后生效。")

    def assign_checker(self):
        """Authorize the demo checker for the selected session."""
        session = self.selected()["session_id"]
        checker = next(row for row in self.users if row["role"] == "checker")
        self.service.assign_checker(self.actor["id"], checker["id"], session)
        self.status.set(f"已授权 {checker['name']} 核验所选场次")

    def register(self, selected):
        """Collect ticket choices and offer the waitlist if stock is short."""
        session = selected["session_id"]
        actor = self.actor["id"]
        types = self.service.ticket_types(actor, session)
        if not types:
            raise RuleError("该场次暂无可选票种。")
        options = {
            (
                f"{row['name']} · ¥{row['price_cents'] / 100:.2f}"
                f" · 余 {row['available']} 张"
            ): row["id"]
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
        """Show current offers and allow the participant to respond."""
        actor = self.actor["id"]
        window, frame, table = self.table_window("我的候补", (
            ("活动", 135), ("场次", 275), ("票种", 140),
            ("状态", 85), ("确认截止", 170),
        ), height=10)
        rows = {}

        def refresh():
            data = self.service.my_waitlist(actor)
            selected = table.selection()
            table.delete(*table.get_children())
            rows.clear()
            for row in data:
                key = str(row["id"])
                rows[key] = row
                deadline = (
                    row["deadline"] if row["status"] == "offered" else None
                )
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
                    f"{row['title']} · {self.session_label(row)}\n"
                    f"{row['types']}\n合计 ¥{amount:.2f}",
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

        waitlist_actions = ttk.Frame(frame)
        waitlist_actions.pack(anchor="e", pady=(10, 0))
        for label, callback in (
            ("刷新", refresh), ("确认递补", lambda: respond(True)),
            ("放弃候补", lambda: respond(False)),
        ):
            ttk.Button(
                waitlist_actions, text=label,
                command=lambda fn=callback: self.run(fn, parent=window),
            ).pack(side="left", padx=(5, 0))
        refresh()
        window.wait_visibility()
        window.grab_set()

    def show_tickets(self):
        """List registrations and show each ticket in the selected one."""
        actor = self.actor["id"]
        tickets = self.service.my_tickets(actor)
        if not tickets:
            self.show_notice("我的电子票", "当前没有电子票。")
            return

        window, frame = self.dialog_panel("我的电子票")
        window.geometry("880x500")
        window.minsize(840, 465)
        footer = ttk.Frame(frame)
        footer.pack(side="bottom", fill="x", pady=(10, 0))
        feedback = ttk.Label(footer)
        feedback.pack(side="left", fill="x", expand=True)
        cancel_button = ttk.Button(
            footer, text="取消整单", style="Danger.TButton", state="disabled",
        )
        cancel_button.pack(side="right")

        detail = ttk.Frame(frame, padding=(12, 0, 0, 0))
        detail.pack(side="right", fill="both")
        ttk.Label(detail, text="报名详情", style="ListTitle.TLabel").pack(
            anchor="w", pady=(0, 8),
        )
        summary = ttk.Label(
            detail, width=42, wraplength=345, padding=8,
            style="CardDetail.TLabel", justify="left",
        )
        summary.pack(fill="x", pady=(0, 8))
        ticket_view = CardList(detail)
        ticket_view.pack(fill="both", expand=True)
        cards = ticket_view.body

        def scroll_tickets(event):
            if str(event.widget).startswith(f"{ticket_view}."):
                if ticket_view.scroll(event.delta):
                    return "break"
            return None

        window.bind("<MouseWheel>", scroll_tickets)
        ttk.Separator(frame, orient="vertical").pack(side="right", fill="y")

        orders_panel = ttk.Frame(frame, padding=(0, 0, 12, 0))
        orders_panel.pack(side="left", fill="both", expand=True)
        ttk.Label(
            orders_panel, text="报名记录", style="ListTitle.TLabel",
        ).pack(anchor="w", pady=(0, 8))
        orders_table = self.build_table(orders_panel, (
            ("报名编号", 72), ("活动名称", 148),
            ("场次时间", 128), ("报名状态", 92),
        ), height=9)
        orders = {}

        def show_order(_event=None):
            ticket_view.clear()
            selection = orders_table.selection()
            rows = orders.get(selection[0], []) if selection else []
            cancel_button.configure(state="disabled")
            if not rows:
                summary.configure(text="选择一笔报名查看电子票。")
                feedback.configure(text="")
                return

            first = rows[0]
            state = self.ticket_order_status(rows)
            payment = self.payment_label(first)
            summary.configure(text=(
                f"报名 #{first['registration_id']} · {first['title']}\n"
                f"{self.session_label(first)}\n"
                f"{state} · {len(rows)} 张票 · ¥{first['amount'] / 100:.2f}\n"
                f"{payment}"
            ))
            now = self.service.clock()
            for ticket in rows:
                self.add_ticket_card(cards, ticket, now, feedback)

            starts_at = datetime.fromisoformat(first["starts_at"])
            remaining = starts_at - now
            can_cancel = (
                first["registration_status"] == "registered"
                and not any(ticket["checked_at"] for ticket in rows)
                and not first["cancelled_at"]
                and first["event_status"] != "cancelled"
                and remaining > SELF_CANCELLATION_CUTOFF
            )
            if can_cancel:
                cancel_button.configure(state="normal")
            hint = ""
            if state == "已取消":
                hint = "这笔报名已取消，电子票已失效。"
            elif not can_cancel and state == "已报名":
                hint = (
                    "距开场不足或等于 24 小时，可联系组织者代取消。"
                    if remaining > timedelta(0) else "场次已开始，无法取消报名。"
                )
            feedback.configure(text=hint)

        def load_orders(records, selected=None):
            orders.clear()
            orders_table.delete(*orders_table.get_children())
            for ticket in records:
                order = str(ticket["registration_id"])
                orders.setdefault(order, []).append(ticket)
            for order, rows in orders.items():
                first = rows[0]
                orders_table.insert("", "end", iid=order, values=(
                    f"#{order}", first["title"],
                    first["starts_at"].replace("T", " ")[:16],
                    self.ticket_order_status(rows),
                ))
            target = (
                selected if selected in orders else next(iter(orders), None)
            )
            if target is not None:
                orders_table.selection_set(target)
                orders_table.see(target)
            show_order()

        def cancel_selected():
            selection = orders_table.selection()
            if not selection or cancel_button.instate(("disabled",)):
                return
            order = selection[0]
            if self.cancel_my_order(
                window, actor, int(order), len(orders[order]),
            ):
                load_orders(self.service.my_tickets(actor), order)
                feedback.configure(text=f"报名 #{order} 已取消，名额已释放。")

        orders_table.bind("<<TreeviewSelect>>", show_order)
        cancel_button.configure(
            command=lambda: self.run(cancel_selected, parent=window),
        )
        load_orders(tickets)
        window.wait_visibility()
        window.grab_set()

    @staticmethod
    def ticket_order_status(rows):
        """Summarize attendance across the tickets in a registration."""
        first = rows[0]
        status = first["registration_status"]
        if status == "cancelled":
            return "已取消"
        if status == "checked_in":
            all_checked = all(ticket["checked_at"] for ticket in rows)
            return "已签到" if all_checked else "部分已签到"
        if first["cancelled_at"] or first["event_status"] == "cancelled":
            return "已取消"
        return "缺席" if status == "absent" else "已报名"

    def add_ticket_card(self, parent, ticket, now, feedback):
        """Show a ticket's attendance status and offer to copy its code."""
        card = ttk.Frame(parent, padding=(10, 7), style="SessionCard.TFrame")
        card.pack(fill="x", pady=(0, 6))
        ends_at = ticket["ends_at"]
        status = ticket["registration_status"]
        if status == "cancelled":
            state = "已取消 · 票据失效"
        elif ticket["checked_at"]:
            checked_at = ticket["checked_at"].replace("T", " ")
            state = f"已签到 · {checked_at}"
        elif ticket["cancelled_at"] or ticket["event_status"] == "cancelled":
            state = "场次已取消 · 票据失效"
        elif status == "absent" or (
            ends_at and datetime.fromisoformat(ends_at) <= now
        ):
            state = "缺席"
        else:
            state = "尚未签到"
        ttk.Label(
            card, style="CardTitle.TLabel", wraplength=315,
            text=f"{ticket['name']} · ¥{ticket['price_cents'] / 100:.2f}",
        ).pack(anchor="w")
        ttk.Label(
            card, style="CardDetail.TLabel", wraplength=315,
            text=f"持票人 {ticket['holder_name']}\n{state}",
        ).pack(anchor="w", pady=(2, 5))

        def copy_code():
            parent.clipboard_clear()
            parent.clipboard_append(ticket["code"])
            feedback.configure(text="已复制电子票码。")

        code = ttk.Frame(card, style="SessionCard.TFrame")
        code.pack(fill="x")
        ttk.Button(code, text="复制票码", command=copy_code).pack(side="right")
        field = ttk.Entry(code, width=24)
        field.insert(0, ticket["code"])
        field.state(("readonly",))
        field.pack(side="left", fill="x", expand=True, padx=(0, 6))

    def cancel_my_order(self, window, actor, order, ticket_count):
        """Confirm and cancel only the selected registration."""
        if not self.confirm_action(
            "取消整单",
            f"确认取消报名 #{order}？该报名的 {ticket_count} 张电子票将全部失效。",
            parent=window, label="确认取消",
        ):
            return False
        self.service.cancel_registration(actor, order)
        self.refresh()
        self.status.set(f"报名 #{order} 已取消，名额已释放")
        return True

    def assisted_cancel(self, *, parent=None, order=None):
        """Collect the reason for cancelling another person's order."""
        registrations = self.service.session_registrations(
            self.actor["id"], self.selected()["session_id"],
        )
        eligible = [
            row for row in registrations if row["status"] == "registered"
        ]
        if not eligible:
            raise RuleError("所选场次没有未签到的有效报名。")
        choices = {str(row["id"]): row["name"] for row in eligible}
        if order is not None and str(order) not in choices:
            raise RuleError("请选择当前场次的有效报名编号。")
        fields = [("代取消原因", "")]
        if order is None:
            fields.insert(0, ("报名编号", tuple(choices)))
            message = "\n".join(
                f"#{key}  {name}" for key, name in choices.items()
            )
        else:
            message = f"报名 #{order} · {choices[str(order)]}"
        result = self.form("代取消报名", fields, parent=parent, message=message)
        if result is None:
            return False
        order = order if order is not None else int(result[0])
        self.service.cancel_registration(
            self.actor["id"], order, reason=result[-1],
        )
        self.status.set(f"报名 #{order} 已人工取消")
        return True

    def update_checker_session(self, _event=None):
        """Reset the ticket input when the assigned session changes."""
        if self.actor["role"] != "checker":
            return
        selection = self.table.selection()
        row = self.sessions[int(selection[0])] if selection else None
        session = row["session_id"] if row is not None else None
        if session != self.checker_session_id:
            self.checker_session_id = session
            self.checker_code.set("")
        self.checker_title.configure(
            text=row["title"] if row is not None else "尚未选择场次",
        )
        self.checker_details.configure(
            text=(
                self.session_label(row) if row is not None else "请从左侧选择已授权场次。"
            ),
        )
        state = "normal" if row is not None else "disabled"
        self.code_entry.configure(state=state)
        self.verify_ticket_button.configure(state=state)
        self.update_code_placeholder()

    def update_code_placeholder(self, _event=None):
        """Show the entry hint only while the empty input lacks focus."""
        if (self.checker_code.get() or self.root.focus_get() == self.code_entry
                or self.code_entry.instate(("disabled",))):
            self.code_placeholder.place_forget()
        else:
            self.code_placeholder.place(x=10, rely=0.5, anchor="w")

    def fit_checker_result(self, event):
        """Wrap the result text without encroaching on the button."""
        width = (
            event.width - self.confirm_check_in_button.winfo_reqwidth() - 34
        )
        self.checker_result_details.configure(wraplength=max(100, width))

    def set_checker_result(self, title, detail, *, style="CardTitle.TLabel"):
        """Update the status heading and detail text in the checker panel."""
        self.checker_result_title.configure(text=title, style=style)
        self.checker_result_details.configure(text=detail)

    def reset_ticket_preview(self, *_args):
        """Clear the verified ticket and disable confirmation."""
        self.verified_ticket = None
        self.confirm_check_in_button.configure(state="disabled")
        self.update_code_placeholder()
        self.set_checker_result("待核验", "核验通过后可确认签到。")

    def show_duplicate_ticket(self, exc):
        """Show the original check-in time and disable confirmation."""
        self.confirm_check_in_button.configure(state="disabled")
        self.set_checker_result(
            "该电子票已完成签到",
            f"该票已于 {exc.first_checked_at} 完成首次签到，请勿重复操作。",
            style="CheckDuplicate.TLabel",
        )

    def preview_ticket(self):
        """Display the holder and ticket type before confirming check-in."""
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
        """Record attendance only for the currently verified ticket."""
        self.update_checker_session()
        if self.verified_ticket is None:
            return
        session, code = self.verified_ticket
        self.reset_ticket_preview()
        if (self.actor["role"] != "checker"
                or session != self.checker_session_id
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
        """Display refund and paid status ahead of fee requirements."""
        if row["refunded_at"]:
            return "已退"
        if row["paid_at"]:
            return "已线下缴费"
        if row["amount"] == 0:
            return "无需缴费"
        return "待缴费"

    def dialog_panel(self, title):
        """Open a window with a padded content area."""
        window = tk.Toplevel(self.root)
        window.title(title)
        window.transient(self.root)
        panel = ttk.Frame(window, padding=16)
        panel.pack(fill="both", expand=True)
        return window, panel

    def table_window(self, title, columns, *, height=12):
        """Create a padded dialog with a scrollable table inside."""
        window, panel = self.dialog_panel(title)
        return window, panel, self.build_table(panel, columns, height=height)

    def build_table(self, panel, columns, *, height=12, anchor="w"):
        """Build a single-selection table from column labels and widths."""
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
            table.column(name, width=width, minwidth=50, anchor=anchor)
        return table

    def show_payments(self):
        """Manage payment, refund, and cancellation for one session."""
        actor, session = self.actor["id"], self.selected()["session_id"]
        rows = self.service.session_payments(actor, session)
        window, panel, table = self.table_window("报名管理", (
            ("报名编号", 90), ("报名人", 130), ("金额", 95),
            ("报名状态", 95), ("费用状态", 130),
        ))
        states = {
            "registered": "已报名", "checked_in": "已签到",
            "absent": "缺席", "cancelled": "已取消",
        }

        def render(records):
            table.delete(*table.get_children())
            for row in records:
                table.insert("", "end", iid=str(row["id"]), values=(
                    row["id"], row["name"], f"¥{row['amount'] / 100:.2f}",
                    states[row["status"]],
                    self.payment_label(row) if row["amount"] else "免费",
                ))

        def selected_order():
            selection = table.selection()
            if not selection:
                raise RuleError("请先选择一笔报名。")
            return int(selection[0])

        def update(refund):
            order = selected_order()
            verb = "退款" if refund else "缴费"
            if self.confirm_action(
                f"登记{verb}", f"确认线下已完成报名 #{order} 的整单{verb}？", parent=window,
            ):
                self.service.record_payment(actor, order, refund=refund)
                render(self.service.session_payments(actor, session))

        def cancel():
            if self.assisted_cancel(parent=window, order=selected_order()):
                render(self.service.session_payments(actor, session))

        render(rows)
        buttons = ttk.Frame(panel)
        buttons.pack(anchor="e", pady=(10, 0))
        ttk.Button(
            buttons, text="代取消报名",
            command=lambda: self.run(cancel, parent=window),
        ).pack(side="left")
        for name, refund in (("登记缴费", False), ("登记退款", True)):
            ttk.Button(
                buttons, text=name,
                command=lambda flag=refund: self.run(
                    lambda: update(flag), parent=window,
                ),
            ).pack(side="left", padx=(5, 0))
        window.wait_visibility()
        window.grab_set()

    def show_notices(self):
        """List the signed-in participant's in-app notices."""
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
        """Query and display audit records with details for the selection."""
        actor = self.actor["id"]
        self.log_page = ttk.Frame(self.logs_container)
        self.log_page.pack(fill="both", expand=True)
        filters = ttk.Frame(self.log_page)
        filters.pack(fill="x", pady=(0, 10))
        content = ttk.Frame(self.log_page)
        content.pack(fill="both", expand=True)
        detail = ttk.Frame(content, padding=(12, 0, 0, 0))
        detail.pack(side="right", fill="y")
        ttk.Label(detail, text="日志详情", style="ListTitle.TLabel").pack(
            anchor="w", pady=(0, 8),
        )
        description = ttk.Label(
            detail, style="CardDetail.TLabel", width=21, wraplength=190,
            padding=(9, 8), justify="left",
        )
        description.pack(fill="x")
        ttk.Separator(content, orient="vertical").pack(
            side="right", fill="y", padx=(10, 0),
        )
        table = self.build_table(content, (
            ("时间", 140), ("操作", 104), ("操作人", 90),
            ("操作对象", 91),
        ))
        fields = {}
        target_types = {
            "全部": "",
            **{name: kind for kind, name in LOG_TARGET_NAMES.items()},
        }
        labels = (("action", "操作"), ("operator", "操作人"),
                  ("target_type", "对象类型"),
                  ("target", "对象编号"), ("date", "日期"))
        for index, (key, name) in enumerate(labels):
            row, column = divmod(index, 3)
            ttk.Label(filters, text=name).grid(
                row=row, column=2 * column, sticky="w", pady=3,
            )
            if key == "target_type":
                widget = ttk.Combobox(
                    filters, state="readonly", values=tuple(target_types),
                    width=7,
                )
                widget.current(0)
            else:
                widget = ttk.Entry(filters, width=11 if key == "date" else 9)
            widget.grid(
                row=row, column=2 * column + 1, sticky="w", padx=(4, 12),
            )
            fields[key] = widget
        entries = {}

        def show_detail(_event=None):
            selection = table.selection()
            item = entries.get(selection[0]) if selection else None
            if item is None:
                description.configure(text="选择一条记录查看详情。")
                return
            row, target, event = item
            description.configure(text=(
                f"{row['action']}\n{row['created_at'].replace('T', ' ')}\n\n"
                f"操作人  {row['name']}\n操作对象  {target}\n"
                f"关联活动  {event}\n\n原因\n{row['reason'] or '—'}"
            ))

        table.bind("<<TreeviewSelect>>", show_detail)

        def search():
            query = {key: field.get().strip() for key, field in fields.items()}
            query["target_type"] = target_types[query["target_type"]]
            logs = self.service.operation_history(actor, **query)
            table.delete(*table.get_children())
            entries.clear()
            for row in logs:
                kind = LOG_TARGET_NAMES.get(row["target_type"], "其他")
                event = (
                    f"#{row['event_id']} · {row['event_title']}"
                    if row["event_id"] is not None else "—"
                )
                target = f"{kind} #{row['target_id']}"
                item = table.insert("", "end", values=(
                    row["created_at"].replace("T", " "), row["action"],
                    row["name"], target,
                ))
                entries[item] = row, target, event
            if entries:
                table.selection_set(next(iter(entries)))
            show_detail()
            self.status.set(f"共 {len(logs)} 条记录 · 最多显示最近 500 条")

        ttk.Button(
            filters, text="查询", style="Primary.TButton",
            command=lambda: self.run(search),
        ).grid(row=1, column=4, columnspan=2, sticky="w")
        fields["date"].bind("<Return>", lambda _: self.run(search))
        search()

    def show_session_report(self):
        """Show attendance totals and export a session roster."""
        session = self.selected()["session_id"]
        actor = self.actor["id"]
        snapshot = self.service.session_report(actor, session)
        details = snapshot[0]
        window, panel = self.dialog_panel("场次统计与导出")
        heading = ttk.Label(
            panel, text=f"{details['title']} · {self.session_label(details)}",
            style="ListTitle.TLabel",
        )
        heading.pack(anchor="w", pady=(0, 6))
        summary = tk.StringVar(window)
        ttk.Label(panel, textvariable=summary).pack(anchor="w", pady=(0, 10))
        headings = (
            "票种", "票种状态", "容量", "已用", "预留", "余票",
            "已签到", "签到率", "缺席", "缺席率",
        )
        table = self.build_table(
            panel, [(name, 105 if name == "票种" else 68) for name in headings],
            height=8, anchor="center",
        )

        def render(session_info, rows, tickets, ended):
            checked = sum(row["checked"] for row in rows)
            absence_state = "场次已结束" if ended else "场次未结束或未设置结束时间"
            if session_info["cancelled_at"]:
                absence_state = "场次已取消"
            summary.set(
                f"有效票 {len(tickets)} 张，已签到 {checked} 张 · {absence_state}"
            )
            table.delete(*table.get_children())
            for row in rows:
                count = row["issued"]
                absent = row["absent"]
                checked_rate = (
                    f"{row['checked'] / count:.1%}" if count else "—"
                )
                absent_rate = (
                    f"{absent / count:.1%}"
                    if count and absent is not None else "—"
                )
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

        render(*snapshot)
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
        """Return whether the confirmation dialog was accepted."""
        return self.form(
            title, (), parent=parent, message=message, confirm_label=label,
        ) is not None

    def show_notice(self, title, message, *, parent=None):
        """Display a modal notice with a single dismiss button."""
        self.form(
            title, (), parent=parent, message=message, confirm_label="关闭",
            show_cancel=False,
        )

    def form(
        self, title, fields, *, parent=None, multiline_last=False, message="",
        multiline_hint="",
        on_submit=None, confirm_label="确认", show_cancel=True,
    ):
        """Return the validated field values, or None when dismissed."""
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
        widgets = self.build_form_fields(
            panel, fields, multiline_last, multiline_hint,
        )
        submissions: list[list[str]] = []

        def confirm():
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
                except (
                    ValueError, InvalidOperation, OSError, sqlite3.Error,
                ) as exc:
                    self.show_notice("无法保存", str(exc), parent=dialog)
                    return
            submissions.append(values)
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
        return next(iter(submissions), None)

    @staticmethod
    def build_form_fields(panel, fields, multiline_last, multiline_hint):
        """Build input widgets in the order the caller supplies fields."""
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
        return widgets

    def wait_for_modal(self, dialog, parent, focus):
        """Show the dialog and restore its parent's focus and grab."""
        previous_grab = parent.grab_current()
        previous_focus = parent.focus_get()
        dialog.bind("<Escape>", lambda _: dialog.destroy())
        dialog.tk.call("::tk::PlaceWindow", str(dialog), "widget", str(parent))
        dialog.wait_visibility()
        dialog.grab_set()
        focus.focus_set()
        parent.wait_window(dialog)
        if previous_grab is not None and previous_grab.winfo_exists():
            previous_grab.grab_set()
        if previous_focus is not None and previous_focus.winfo_exists():
            previous_focus.focus_set()


def main():
    """Initialize the chosen SQLite database and launch the desktop UI."""
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
