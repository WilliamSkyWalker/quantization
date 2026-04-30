"""回测结果 Django Model"""

from django.db import models


class BacktestResult(models.Model):
    market = models.CharField(max_length=10)
    strategy_type = models.CharField(max_length=20)
    start_date = models.CharField(max_length=20)
    end_date = models.CharField(max_length=20)
    summary = models.TextField(blank=True, null=True)
    nav = models.TextField(blank=True, null=True)
    benchmark = models.TextField(blank=True, null=True)
    trades = models.TextField(blank=True, null=True)
    monthly = models.TextField(blank=True, null=True)
    drawdown = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        db_table = "backtest_result"
