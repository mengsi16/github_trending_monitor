"""Cron 调度器 (s07)"""
import time
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from src.config import config

class SchedulerService:
    """定时任务服务"""

    def __init__(self):
        self.scheduler = BackgroundScheduler()
        self.jobs = {}

    def add_job(self, job_id: str, func, cron_expr: str):
        """添加定时任务"""
        trigger = CronTrigger.from_crontab(cron_expr)
        job = self.scheduler.add_job(func, trigger, id=job_id, replaceExisting=True)
        self.jobs[job_id] = job
        return job

    def start(self):
        """启动调度器"""
        if not self.scheduler.running:
            self.scheduler.start()

    def stop(self):
        """停止调度器"""
        if self.scheduler.running:
            self.scheduler.shutdown()

    def list_jobs(self):
        """列出所有任务"""
        return [(job.id, job.next_run_time) for job in self.scheduler.get_jobs()]

# 全局调度器实例
scheduler = SchedulerService()

def setup_cron_jobs(crawler_func, summarizer_func):
    """设置定时任务"""
    scheduler.add_job("github-trending", crawler_func, config.crawler_cron)
    scheduler.add_job("generate-summaries", summarizer_func, config.summarizer_cron)
