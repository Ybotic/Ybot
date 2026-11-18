"""Database-only daily and weekly summaries, grouped by class."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import discord

from config import Config
from database.activity import ActivityRepository
from services.reminder_format import parse_utc_timestamp


@dataclass(frozen=True, slots=True)
class Overview:
    title: str
    sections: dict[str, list[str]]
    summary: str

    def as_dict(self) -> dict:
        return {"title": self.title, "summary": self.summary, "sections": self.sections}

    def embeds(self, marker: str | None = None) -> list[discord.Embed]:
        embed = discord.Embed(title=self.title, description=self.summary, color=discord.Color.blurple())
        for title, lines in self.sections.items():
            # Bound every field and the overall embed while retaining section totals.
            shown: list[str] = []
            for line in lines:
                cleaned = discord.utils.escape_markdown(discord.utils.escape_mentions(line))
                if sum(map(len, shown)) + len(cleaned) > 390:
                    break
                shown.append(cleaned)
            if len(shown) < len(lines):
                shown.append(f"… {len(lines) - len(shown)} more")
            embed.add_field(name=f"{title} ({len(lines)})", value="\n".join(shown) or "None", inline=False)
        embed.set_footer(text=marker or "YBOT · local database overview")
        return [embed]


class DigestService:
    def __init__(self, repository: ActivityRepository, config: Config) -> None:
        self.repository = repository
        self.config = config

    async def generate(self, kind: str = "daily", *, now: datetime | None = None) -> Overview:
        now = now or datetime.now(timezone.utc)
        zone = ZoneInfo(self.config.timezone_name)
        local_now = now.astimezone(zone)
        today = datetime.combine(local_now.date(), time.min, zone)
        tomorrow = today + timedelta(days=1)
        week_start = today - timedelta(days=today.weekday())
        start = week_start if kind == "weekly" else today
        end = now if kind == "weekly" else tomorrow
        recent_start = start if kind == "weekly" else now - timedelta(days=1)
        upcoming_end = now + timedelta(days=7)
        data = await self.repository.overview_data()
        tasks = data["todos"]
        reminders = data["reminders"]

        def between(value: str | None, lower: datetime, upper: datetime) -> bool:
            return value is not None and lower <= parse_utc_timestamp(value) < upper

        def label(row: dict, text_key: str = "title", date_key: str | None = None) -> str:
            group = row.get("class_name") or "Personal"
            text = " ".join(str(row.get(text_key) or "Untitled").split())[:75]
            date = ""
            if date_key and row.get(date_key):
                due = parse_utc_timestamp(row[date_key]).astimezone(zone)
                date = f" · {due:%b %d %I:%M %p}"
            return f"[{group}] #{row['id']} {text}{date}"

        pending = [row for row in tasks if row["status"] == "open"]
        completed = [row for row in tasks if row["status"] == "completed" and between(row["completed_at"], recent_start, now + timedelta(microseconds=1))]
        overdue_tasks = [row for row in pending if row["due_at"] and parse_utc_timestamp(row["due_at"]) < now]
        outstanding_reminders = [row for row in reminders if row["status"] in {"pending", "notified"}]
        today_reminders = [row for row in reminders if row["status"] != "cancelled" and between(row["due_at"], today, tomorrow)]
        overdue_reminders = [row for row in outstanding_reminders if parse_utc_timestamp(row["due_at"]) < now]
        due_today = [row for row in pending if between(row["due_at"], today, tomorrow)]
        high = [row for row in pending if row["priority"] == "high"]
        upcoming = [label(row, date_key="due_at") for row in pending if between(row["due_at"], now, upcoming_end)]
        upcoming += [label(row, "description", "due_at") for row in reminders if row["status"] == "pending" and between(row["due_at"], now, upcoming_end)]
        sections = {
            "Today's reminders": sorted(label(row, "description", "due_at") for row in today_reminders),
            "Overdue reminders": sorted(label(row, "description", "due_at") for row in overdue_reminders),
            "Today's pending tasks": sorted(label(row, date_key="due_at") for row in due_today),
            "Overdue tasks": sorted(label(row, date_key="due_at") for row in overdue_tasks),
            "High-priority tasks": sorted(label(row, date_key="due_at") for row in high),
            "Recently completed tasks": sorted(label(row) for row in completed),
            "Upcoming deadlines · next 7 days": sorted(upcoming),
            "Recently saved notes": sorted(label(row) for row in data["notes"] if between(row["created_at"], recent_start, now + timedelta(microseconds=1))),
            "Recently saved links": sorted(label(row) for row in data["links"] if between(row["saved_at"], recent_start, now + timedelta(microseconds=1))),
        }
        if kind == "weekly":
            missed = [row for row in overdue_reminders if between(row["due_at"], start, now)]
            finished = [row for row in reminders if row["status"] == "completed" and between(row.get("completed_at"), start, now + timedelta(microseconds=1))]
            progress: dict[str, list[int]] = {}
            for row in pending:
                progress.setdefault(row.get("class_name") or "Personal", [0, 0])[0] += 1
            for row in completed:
                progress.setdefault(row.get("class_name") or "Personal", [0, 0])[1] += 1
            sections = {
                "Tasks completed this week": sections["Recently completed tasks"],
                "Tasks still pending": sorted(label(row, date_key="due_at") for row in pending),
                "Overdue tasks": sections["Overdue tasks"],
                "Reminders completed this week": sorted(label(row, "description") for row in finished),
                "Unresolved reminders due this week": sorted(label(row, "description", "due_at") for row in missed),
                "Class-wise progress": [f"[{name}] {counts[1]} completed · {counts[0]} pending" for name, counts in sorted(progress.items())],
                "Upcoming deadlines · next 7 days": sections["Upcoming deadlines · next 7 days"],
                "Recently saved notes": sections["Recently saved notes"],
                "Recently saved links": sections["Recently saved links"],
            }
        activity = any(sections.values())
        summary = (
            f"{len(completed)} tasks completed {'this week' if kind == 'weekly' else 'in the last 24 hours'} · "
            f"{len(pending)} open · {len(overdue_tasks)} overdue · {len(high)} high priority.\n"
            f"Times: {self.config.timezone_name}."
            if activity else "Nothing scheduled or overdue. Enjoy a clear slate!"
        )
        return Overview(
            title=f"YBOT {'Weekly Review' if kind == 'weekly' else 'Daily Digest'} · {local_now:%B %d, %Y}",
            sections=sections,
            summary=summary,
        )
