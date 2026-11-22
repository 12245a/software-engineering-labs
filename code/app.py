"""Tkinter entry point for the activity ticketing prototype."""

from argparse import ArgumentParser
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sqlite3
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from ticketing import RuleError, SoldOut, TicketRequest, Ticketing

STATES = {
    "draft": "草稿", "pending": "待审核", "published": "已发布",
    "unlisted": "已下架", "disabled": "已停用", "cancelled": "已取消",
}
WAITLIST_STATES = {
    "queued": "候补中", "offered": "待确认", "confirmed": "已确认", "closed": "已关闭",
}


class Desktop:
    def __init__(self, root, service):
        self.root = root
        self.service = service
        self.users = service.users()
        self.actor = self.users[0]
        self.sessions = []
        self.filters = {}
        root.title("活动票务与报名系统")
        root.geometry("930x570")
        root.minsize(760, 420)
        root.configure(bg="#edf4ff")
        panel = ttk.Frame(root, padding=18)
        panel.pack(fill="both", expand=True)
        ttk.Label(panel, text="活动票务与报名系统", font=("Microsoft YaHei UI", 20)).pack(
            anchor="w", pady=(0, 12)
        )
        bar = ttk.Frame(panel)
        bar.pack(fill="x", pady=(0, 12))
        ttk.Label(bar, text="演示身份").pack(side="left")
        self.account = ttk.Combobox(
            bar,
            state="readonly",
            values=[row["name"] for row in self.users],
            width=18,
        )
        self.account.pack(side="left", padx=8)
        self.account.current(0)
        self.account.bind("<<ComboboxSelected>>", self.switch_account)
        self.search = ttk.Entry(bar, width=22)
        self.search.pack(side="left", padx=(12, 5))
        self.search.bind("<Return>", lambda _: self.run(self.refresh))
        ttk.Button(bar, text="搜索 / 刷新", command=lambda: self.run(self.refresh)).pack(side="left")
        self.actions = ttk.Frame(panel)
        self.actions.pack(fill="x", pady=(0, 10))
        columns = ("title", "status", "venue", "start", "reason")
        self.table = ttk.Treeview(panel, columns=columns, show="headings", selectmode="browse")
        for column, label, width in zip(
            columns, ("活动", "状态", "地点", "开始时间", "审核原因"), (180, 70, 100, 160, 140)
        ):
            self.table.heading(column, text=label)
            self.table.column(column, width=width, minwidth=60)
        self.table.pack(fill="both", expand=True)
        self.status = tk.StringVar()
        ttk.Label(panel, textvariable=self.status).pack(anchor="w", pady=(8, 0))
        self.switch_account()
        self.root.after(15000, self.tick_waitlist)

    def tick_waitlist(self):
        try:
            self.service.process_waitlists()
        except sqlite3.Error as exc:
            self.status.set(f"候补处理失败，稍后重试：{exc}")
        finally:
            self.root.after(15000, self.tick_waitlist)

    def run(self, action, *, parent=None):
        try:
            action()
        except (ValueError, InvalidOperation, OSError, sqlite3.Error) as exc:
            messagebox.showerror("操作失败", str(exc), parent=parent or self.root)

    def switch_account(self, _event=None):
        self.actor = self.users[self.account.current()]
        self.filters.clear()
        for widget in self.actions.winfo_children():
            widget.destroy()
        actions = {
            "organizer": [
                ("创建活动", self.create_event),
                ("提交审核", self.submit_review),
                ("提交变更", self.submit_change),
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
                ("报名", self.register), ("我的电子票", self.show_tickets),
                ("我的候补", self.show_waitlist),
                ("我的通知", self.show_notices),
                ("筛选场次", self.filter_sessions),
            ],
            "checker": [("输入票码核验", self.check_in)],
        }
        for label, action in actions[self.actor["role"]]:
            ttk.Button(
                self.actions, text=label, command=lambda callback=action: self.run(callback)
            ).pack(side="left", padx=(0, 8))
        self.run(self.refresh)

    def refresh(self):
        self.sessions = self.service.list_sessions(
            self.actor["id"], keyword=self.search.get().strip(), **self.filters,
        )
        self.table.delete(*self.table.get_children())
        for index, row in enumerate(self.sessions):
            self.table.insert(
                "", "end", iid=str(index),
                values=(
                    row["title"],
                    "变更待审核" if row["status"] == "pending" and row["is_public"]
                    else STATES[row["status"]],
                    row["venue"],
                    row["starts_at"].replace("T", " "), row["review_reason"] or "",
                ),
            )
        if self.sessions:
            self.table.selection_set("0")
        self.status.set(f"共 {len(self.sessions)} 个场次")

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

    def create_event(self):
        default_start = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
        result = self.form("创建活动草稿", [
            ("活动名称", ""),
            ("地点", ""),
            ("开始时间（年-月-日 时:分）", default_start),
            ("预计时长（分钟）", "120"),
            ("场次容量", "30"),
            ("票种（名称,价格元,名额；每行一种）", "普通票,0,20\nVIP票,20,10"),
        ], multiline_last=True)
        if result is None:
            return
        title, venue, start, duration, capacity, raw_types = result
        types = []
        for line in raw_types.splitlines():
            if not line.strip():
                continue
            parts = line.split(",")
            if len(parts) != 3:
                raise RuleError("每行票种请填写名称、价格和名额，用英文逗号分隔。")
            name, price, quota = parts
            types.append((name, self.parse_price(price), int(quota)))
        starts_at = datetime.strptime(start, "%Y-%m-%d %H:%M")
        ends_at = starts_at + timedelta(minutes=int(duration))
        self.service.create_event(
            self.actor["id"], title, venue, starts_at, int(capacity), types, ends_at=ends_at,
        )
        self.refresh()

    def submit_review(self):
        self.service.submit_review(self.actor["id"], self.selected()["event_id"])
        self.refresh()

    def manage_event(self):
        event = self.selected()
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
        if not messagebox.askyesno(
            "确认活动操作", f"{action}「{event['title']}」？\n{consequence}", parent=self.root,
        ):
            return
        self.service.set_event_status(actor["id"], event["event_id"], actions[action], reason)
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
            choices = "\n".join(f"{row['id']}：{row['name']}" for row in options)
            selected = simpledialog.askinteger(
                "选择票种", f"请输入本次要变更的票种编号\n{choices}",
                initialvalue=ticket["id"], parent=self.root,
            )
            if selected is None:
                return
            ticket = next((row for row in options if row["id"] == selected), None)
            if ticket is None:
                raise RuleError("请选择当前场次的票种。")
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
        event_id = self.selected()["event_id"]
        actor = self.actor["id"]
        request = self.service.pending_review(actor, event_id)
        summary = ""
        if request["session_id"] is not None:
            old_start = request["old_start"].replace("T", " ")
            start = request["starts_at"].replace("T", " ")
            summary = (
                f"开始时间：{old_start} → {start}\n"
                f"场次容量：{request['old_capacity']} → {request['session_capacity']}\n"
                f"{request['name']}容量：{request['old_quota']} → {request['ticket_capacity']}\n"
                f"票价：¥{request['old_price'] / 100:.2f} → ¥{request['price_cents'] / 100:.2f}"
            )
        if approved and summary and not messagebox.askyesno(
            "确认变更", summary + "\n\n是否通过并应用以上变更？", parent=self.root,
        ):
            return
        reason = ""
        if not approved:
            prompt = f"{summary}\n\n请填写驳回原因" if summary else "请填写驳回原因"
            reason = simpledialog.askstring("驳回审核", prompt, parent=self.root)
            if reason is None:
                return
        self.service.review(
            actor, event_id, approved, reason,
            confirm_reduction=bool(summary), review_id=request["review_id"],
        )
        self.refresh()

    def assign_checker(self):
        session = self.selected()["session_id"]
        checker = next(row for row in self.users if row["role"] == "checker")
        self.service.assign_checker(self.actor["id"], checker["id"], session)
        self.status.set(f"已授权 {checker['name']} 核验所选场次")

    def register(self):
        session = self.selected()["session_id"]
        types = self.service.ticket_types(self.actor["id"], session)
        if not types:
            raise RuleError("该场次暂无可选票种。")
        options = "    ".join(
            f"{row['id']} {row['name']} ¥{row['price_cents'] / 100:.2f}（余 {row['available']}）"
            for row in types
        )
        result = self.form("提交报名", [
            ("第一张票的票种编号", str(types[0]["id"])),
            ("第一张票的持票人", self.actor["name"]),
            ("第二张票的票种编号（选填）", ""),
            ("第二张票的持票人（选填）", ""),
        ], message=options)
        if result is None:
            return
        first, name, second, other_name = result
        requests = [TicketRequest(int(first), name)]
        if second.strip() or other_name.strip():
            if not second.strip():
                raise RuleError("请填写第二张票的票种编号。")
            requests.append(TicketRequest(int(second), other_name))
        actor = self.actor["id"]
        try:
            codes = self.service.register(actor, session, requests)
        except SoldOut:
            if not messagebox.askyesno(
                "加入候补",
                "整单名额不足。开场前两小时半起不再发送新递补通知。是否加入候补？",
                parent=self.root,
            ):
                return
            entry = self.service.join_waitlist(actor, session, requests, accept_no_offer=True)
            self.status.set(f"候补 #{entry} 已登记，可在「我的候补」查看")
            return
        self.status.set(f"报名成功，已生成 {len(codes)} 张电子票")
        self.show_tickets()

    def show_waitlist(self):
        actor = self.actor["id"]
        window = tk.Toplevel(self.root)
        window.title("我的候补")
        window.transient(self.root)
        frame = ttk.Frame(window, padding=16)
        frame.pack(fill="both", expand=True)
        columns = ("activity", "types", "status", "deadline")
        table = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        for key, heading in zip(columns, ("活动", "票种", "状态", "确认截止")):
            table.heading(key, text=heading)
            table.column(key, width=160)
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
                    row["title"], row["types"], WAITLIST_STATES[row["status"]],
                    datetime.fromisoformat(deadline).isoformat(sep=" ", timespec="seconds")
                    if deadline else "",
                ))
            if selected and selected[0] in rows:
                table.selection_set(selected[0])

        def respond(accept):
            selected = table.selection()
            if not selected:
                raise RuleError("请先选中一条候补记录。")
            row = rows[selected[0]]
            if not accept and not messagebox.askyesno(
                "放弃候补", "是否放弃这笔候补？已预留的名额也会释放。", parent=window,
            ):
                return
            codes = self.service.respond_to_offer(
                actor, row["id"], accept=accept, expected_deadline=row["deadline"],
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
            messagebox.showinfo("我的电子票", "当前没有电子票。", parent=self.root)
            return
        window = tk.Toplevel(self.root)
        window.title("我的电子票")
        window.transient(self.root)
        frame = ttk.Frame(window, padding=16)
        frame.pack(fill="both", expand=True)
        display = tk.Text(frame, width=66, height=16, wrap="word")
        display.pack(fill="both", expand=True)
        for ticket in tickets:
            cancelled = ticket["registration_status"] == "cancelled"
            checked_at = ticket["checked_at"]
            state = "已取消，票据失效" if cancelled else (
                checked_at.replace("T", " ") if checked_at else "尚未签到"
            )
            display.insert(
                "end",
                f"报名 #{ticket['registration_id']} · {ticket['title']}\n"
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
        order = simpledialog.askinteger(
            "取消整单", "请输入电子票上方显示的报名编号", minvalue=1, parent=window,
        )
        if order is None or not messagebox.askyesno(
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
        self.service.cancel_registration(self.actor["id"], order, reason=result[1])
        self.status.set(f"报名 #{order} 已人工取消")

    def check_in(self):
        session = self.selected()["session_id"]
        actor = self.actor["id"]
        code = simpledialog.askstring("核验电子票", "粘贴电子票的票码", parent=self.root)
        if not code or not code.strip():
            return
        name, ticket_type = self.service.preview_check_in(actor, session, code)
        confirmed = messagebox.askokcancel(
            "电子票核验通过",
            f"持票人：{name}\n票种：{ticket_type}\n\n确认签到吗？",
            default=messagebox.CANCEL, parent=self.root,
        )
        if not confirmed:
            return
        name, timestamp = self.service.check_in(actor, session, code)
        self.status.set(f"{name} 已签到，时间 {timestamp.replace('T', ' ')}")

    @staticmethod
    def payment_label(row):
        if row["refunded_at"]:
            return "已退"
        if row["paid_at"]:
            return "已线下缴费"
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
            table_panel, columns=names, show="headings", selectmode="browse", height=height,
        )
        scrollbar = ttk.Scrollbar(table_panel, orient="vertical", command=table.yview)
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
        states = {"registered": "已报名", "checked_in": "已签到", "cancelled": "已取消"}

        def render(records):
            table.delete(*table.get_children())
            for row in records:
                table.insert("", "end", iid=str(row["id"]), values=(
                    row["id"], row["name"], f"¥{row['amount'] / 100:.2f}", states[row["status"]],
                    self.payment_label(row) if row["amount"] else "免费",
                ))

        def update(refund):
            selection = table.selection()
            if not selection:
                raise RuleError("请先选择一笔报名。")
            order = int(selection[0])
            verb = "退款" if refund else "缴费"
            if messagebox.askyesno(
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
        window, _, table = self.table_window("我的通知", (("时间", 170), ("内容", 520)))
        for row in notices:
            table.insert("", "end", values=(row["created_at"].replace("T", " "), row["message"]))
        window.wait_visibility()
        window.grab_set()

    def show_operation_logs(self):
        actor = self.actor["id"]
        window, panel, table = self.table_window("操作日志", (
            ("时间", 160), ("操作", 145), ("操作人", 125), ("对象编号", 90), ("原因", 240),
        ))
        filters = ttk.Frame(panel)
        filters.pack(fill="x", pady=(10, 0))
        fields = {}
        labels = (("action", "操作"), ("operator", "操作人"), ("target", "对象编号"), ("date", "日期"))
        for key, name in labels:
            ttk.Label(filters, text=name).pack(side="left")
            widget = ttk.Entry(filters, width=12)
            widget.pack(side="left", padx=(4, 8))
            fields[key] = widget

        def search():
            logs = self.service.operation_history(
                actor, **{key: field.get().strip() for key, field in fields.items()},
            )
            table.delete(*table.get_children())
            for row in logs:
                table.insert("", "end", values=(
                    row["created_at"].replace("T", " "), row["action"], row["name"],
                    row["target_id"], row["reason"],
                ))

        ttk.Button(
            filters, text="查询", command=lambda: self.run(search, parent=window),
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
        details, statistics, roster, finished = self.service.session_report(actor, session)
        window = tk.Toplevel(self.root)
        window.title("场次统计与导出")
        window.transient(self.root)
        panel = ttk.Frame(window, padding=16)
        panel.pack(fill="both", expand=True)
        heading = ttk.Label(panel, text=f"{details['title']} · {details['venue']}")
        heading.pack(anchor="w", pady=(0, 6))
        summary = tk.StringVar(window)
        ttk.Label(panel, textvariable=summary).pack(anchor="w", pady=(0, 10))
        headings = ("票种", "容量", "已用", "预留", "余票", "已签到", "签到率", "缺席", "缺席率")
        table = ttk.Treeview(panel, columns=headings, show="headings", height=8)
        for name in headings:
            table.heading(name, text=name)
            table.column(name, width=105 if name == "票种" else 68, anchor="center")
        table.pack(fill="both", expand=True)

        def render(rows, tickets, ended):
            checked = sum(row["checked"] for row in rows)
            absence_state = "场次已结束" if ended else "场次未结束或未设置结束时间"
            summary.set(f"有效票 {len(tickets)} 张，已签到 {checked} 张 · {absence_state}")
            table.delete(*table.get_children())
            for row in rows:
                count = row["issued"]
                absent = row["absent"]
                checked_rate = f"{row['checked'] / count:.1%}" if count else "—"
                absent_rate = f"{absent / count:.1%}" if count and absent is not None else "—"
                table.insert("", "end", values=(
                    row["name"], row["capacity"], count, row["reserved"], row["available"],
                    row["checked"], checked_rate,
                    absent if absent is not None else "—", absent_rate,
                ))

        def refresh():
            _, rows, tickets, ended = self.service.session_report(actor, session)
            render(rows, tickets, ended)

        def export():
            path = filedialog.asksaveasfilename(
                parent=window, title="保存报名名单", defaultextension=".csv",
                filetypes=[("CSV 文件", "*.csv")], initialfile=f"场次{session}_报名名单.csv",
            )
            if path:
                count = self.service.export_roster(actor, session, path)
                messagebox.showinfo("导出完成", f"已导出 {count} 张有效票的记录。", parent=window)

        render(statistics, roster, finished)
        buttons = ttk.Frame(panel)
        buttons.pack(anchor="e", pady=(10, 0))
        for label, callback in (("刷新", refresh), ("导出报名名单", export)):
            ttk.Button(buttons, text=label, command=lambda fn=callback: self.run(
                fn, parent=window,
            )).pack(side="left", padx=(5, 0))
        window.wait_visibility()
        window.grab_set()

    def form(self, title, fields, *, multiline_last=False, message=""):
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.transient(self.root)
        panel = ttk.Frame(dialog, padding=16)
        panel.pack(fill="both", expand=True)
        if message:
            ttk.Label(panel, text=message, wraplength=500).pack(anchor="w", pady=(0, 8))
        widgets = []
        for index, (label, default) in enumerate(fields):
            ttk.Label(panel, text=label).pack(anchor="w")
            multiline = multiline_last and index == len(fields) - 1
            if multiline:
                widget = tk.Text(panel, width=50, height=4)
                widget.insert("1.0", default)
            else:
                widget = ttk.Entry(panel, width=50)
                widget.insert(0, default)
            widget.pack(fill="x", pady=(0, 7))
            widgets.append((widget, multiline))
        result = []

        def confirm():
            for widget, multiline in widgets:
                result.append(widget.get("1.0", "end-1c") if multiline else widget.get())
            dialog.destroy()

        buttons = ttk.Frame(panel)
        buttons.pack(anchor="e")
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="left", padx=5)
        ttk.Button(buttons, text="确认", command=confirm).pack(side="left")
        dialog.bind("<Escape>", lambda _: dialog.destroy())
        dialog.wait_visibility()
        dialog.grab_set()
        widgets[0][0].focus_set()
        self.root.wait_window(dialog)
        return result or None


def main():
    parser = ArgumentParser(description="活动票务与报名系统桌面原型")
    parser.add_argument("--db", type=Path, default=Path(__file__).with_name("lab3.sqlite3"))
    args = parser.parse_args()
    root = tk.Tk()
    service = Ticketing(args.db)
    service.seed_demo_users()
    Desktop(root, service)
    root.mainloop()


if __name__ == "__main__":
    main()
