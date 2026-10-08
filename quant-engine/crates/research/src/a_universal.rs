//! Reproducible pair experiment using existing MySQL data, never writes to DB.
use crate::Error;
use chrono::NaiveDate;
use quant_backtest::{
    a_exec::ACostConfig,
    universal::{self, ABar, Day, Strategy},
};
use quant_core::config::Config;
use rayon::prelude::*;
use serde_json::json;
use std::{collections::BTreeMap, path::Path};

pub async fn run(
    config: &Config,
    pairs: &[String],
    start: NaiveDate,
    end: NaiveDate,
    output: &Path,
    capital: f64,
    grid: usize,
) -> Result<(), Error> {
    if start >= end || !capital.is_finite() || capital <= 0.0 || !(2..=1001).contains(&grid) {
        return Err("Invalid dates, capital or grid (2..1001)".into());
    }
    let pairs: Vec<[String; 2]> = pairs
        .iter()
        .map(|p| {
            let codes: Vec<_> = p.split(':').map(str::to_string).collect();
            if codes.len() != 2
                || codes[0] == codes[1]
                || codes.iter().any(|c| {
                    c.len() != 9
                        || !(c.ends_with(".SH") || c.ends_with(".SZ"))
                        || !c[..6].bytes().all(|b| b.is_ascii_digit())
                })
            {
                return Err("Pairs must be distinct CODE.SH:CODE.SZ assets".into());
            }
            Ok([codes[0].clone(), codes[1].clone()])
        })
        .collect::<Result<_, Error>>()?;
    if pairs.is_empty() {
        return Err("No pairs".into());
    }
    let mut cost = ACostConfig::from_a_share(&config.a_share);
    cost.initial_capital = capital;
    if cost.lot_size <= 0
        || [
            cost.buy_commission,
            cost.sell_commission,
            cost.stamp_tax,
            cost.slippage,
            cost.min_commission,
        ]
        .iter()
        .any(|x| !x.is_finite() || *x < 0.0)
        || cost.slippage >= 1.0
    {
        return Err("Invalid execution costs".into());
    }
    std::fs::create_dir_all(output)?;
    if output.join("report.json").exists() {
        return Err("Output already contains a report; use a fresh directory".into());
    }
    let protocol = json!({"market":"cn","start":start,"end":end,"pairs":pairs,"initial_capital":capital,"grid_points":grid,
        "prior":"uniform over 0..1 inclusive; close-to-close frictionless CRP expert wealth; normalized log wealth",
        "execution":"yesterday close target, next open; raw-price 100-share lots via shared a_exec; costs include entry, no final liquidation",
        "costs":{"buy_commission":cost.buy_commission,"sell_commission":cost.sell_commission,"stamp_tax":cost.stamp_tax,"slippage":cost.slippage,"minimum_commission":cost.min_commission,"lot_size":cost.lot_size},
        "scenarios":["zero_cost","configured_cost","double_cost"],
        "limitations":["fixed, currently known surviving stocks; exploratory, not an unbiased universe selection test or untouched out-of-sample",
        "delayed, constrained execution is not the frictionless universal-portfolio theorem",
        "adj_factor ratio credits synthetic shares and fractional cash as a total-return approximation; not an actual corporate-action ledger",
        "missing quotes: stale valuation, skip whole pair rebalance; fail if start/end quotes absent",
        "historical ST and exact exchange limit prices unavailable: conservatively skip whole pair if either open/pre_close moves >=4.5%; actual limit-up queue fills not modeled",
        "stamp tax uses fixed config rate for entire period; no historical tax schedule or market impact",
        "weekly/monthly mean every 5/21 trading sessions; zero-cost still retains lot, delay and open restrictions",
        "hindsight best weight uses entire evaluation window, is an oracle diagnostic only; same execution constraints as other strategies",
        "Sharpe uses zero risk-free rate; net asset value is marked at close without terminal liquidation"]});
    std::fs::write(
        output.join("protocol.json"),
        serde_json::to_vec_pretty(&protocol)?,
    )?;
    std::fs::write(
        output.join("universal_source.rs"),
        include_str!("../../backtest/src/universal.rs"),
    )?;
    std::fs::write(
        output.join("a_universal_source.rs"),
        include_str!("a_universal.rs"),
    )?;
    let pool =
        quant_db::pool::create_pool(&config.database.url(), &config.database.schema, 4).await?;
    let calendar = quant_db::queries::a_read::get_a_trade_cal(&pool, "SSE", start, end).await?;
    let dates: Vec<_> = calendar.iter().map(|r| r.cal_date).collect();
    if dates.len() < 30 {
        return Err("Insufficient exchange calendar".into());
    }
    let codes: std::collections::BTreeSet<_> = pairs.iter().flatten().cloned().collect();
    let mut tasks = tokio::task::JoinSet::new();
    // Pool bounds DB concurrency at four; only a small explicit set is loaded.
    for code in codes {
        let pool = pool.clone();
        tasks.spawn(async move {
            let rows = sqlx::query_as::<_,quant_db::models::a_stock::ADailyPrice>("SELECT * FROM a_daily_price WHERE ts_code=? AND trade_date>=? AND trade_date<=? ORDER BY trade_date")
                .bind(&code).bind(start).bind(end).fetch_all(&pool).await?;
            Ok::<_,Error>((code,rows))
        });
    }
    let mut series = BTreeMap::new();
    let mut coverage_errors = Vec::new();
    let mut raw_csv = String::from("code,date,open,high,low,close,pre_close,vol,adj_factor\n");
    while let Some(result) = tasks.join_next().await {
        let (code, rows) = result??;
        let mut bars = BTreeMap::new();
        for r in rows {
            let values = [
                r.open,
                r.high,
                r.low,
                r.close,
                r.pre_close,
                r.vol,
                r.adj_factor,
            ];
            if values
                .iter()
                .any(|v| v.is_none_or(|x| !x.is_finite() || x <= 0.0))
            {
                return Err(format!("Invalid/null/nontraded bar: {code} {}", r.trade_date).into());
            }
            let [open, high, low, close, pre_close, vol, adj_factor] = values.map(Option::unwrap);
            raw_csv.push_str(&format!(
                "{code},{},{open},{high},{low},{close},{pre_close},{vol},{adj_factor}\n",
                r.trade_date
            ));
            let b = ABar {
                open,
                high,
                low,
                close,
                pre_close,
                vol,
                adj_factor,
                pct_chg: (close / pre_close - 1.0) * 100.0,
                amount: r.amount.unwrap_or(f64::NAN),
                turnover_rate: f64::NAN,
                pe_ttm: f64::NAN,
                pb: f64::NAN,
                ps_ttm: f64::NAN,
                dv_ttm: f64::NAN,
                total_mv: f64::NAN,
                circ_mv: f64::NAN,
            };
            if bars.insert(r.trade_date, b).is_some() {
                return Err(format!("Duplicate bar {code}").into());
            }
        }
        if !bars.contains_key(&dates[0]) || !bars.contains_key(dates.last().unwrap()) {
            coverage_errors.push(format!(
                "{code}: available {:?}..{:?}; requested trading endpoints {}..{}",
                bars.keys().next(),
                bars.keys().next_back(),
                dates[0],
                dates.last().unwrap()
            ));
        }
        tracing::info!(%code,rows=bars.len(),calendar=dates.len(),"Pair data loaded");
        series.insert(code, bars);
    }
    pool.close().await;
    if !coverage_errors.is_empty() {
        return Err(format!(
            "Missing start/end quotes; refusing to shorten window: {}",
            coverage_errors.join("; ")
        )
        .into());
    }
    std::fs::write(output.join("input_prices.csv"), raw_csv)?;
    let reports = pairs.par_iter().map(|codes| -> Result<serde_json::Value,Error> {
        let days:Vec<_>=dates.iter().map(|date|Day {date:*date,bars:[series[&codes[0]].get(date).cloned(),series[&codes[1]].get(date).cloned()]}).collect();
        let mut scenarios=Vec::new();
        for (label,multiplier) in [("zero_cost",0.0),("configured_cost",1.0),("double_cost",2.0)] {
            let mut c=cost.clone(); c.buy_commission*=multiplier;c.sell_commission*=multiplier;c.stamp_tax*=multiplier;c.slippage*=multiplier;c.min_commission*=multiplier;
            let mut strategies=Vec::new();
            for (name,strategy) in [("universal",Strategy::Universal),("daily_50_50",Strategy::Fixed(0.5,1)),("weekly_50_50",Strategy::Fixed(0.5,5)),("monthly_50_50",Strategy::Fixed(0.5,21)),("hold_50_50",Strategy::Hold(0.5)),("hold_first",Strategy::Hold(1.0)),("hold_second",Strategy::Hold(0.0))] {
                let result=universal::simulate(&days,codes,&c,strategy,grid);
                save_curve(output,codes,label,name,&result)?;
                let mut value=serde_json::to_value(&result)?;value.as_object_mut().unwrap().remove("points");value["strategy"]=json!(name);
                strategies.push(value);
            }
            let mut best: Option<(f64,universal::Result)>=None;
            for i in 0..grid {
                let w=i as f64/(grid-1) as f64;
                let result=universal::simulate(&days,codes,&c,Strategy::Fixed(w,1),grid);
                if best.as_ref().is_none_or(|(_,b)|result.total_return>b.total_return) {best=Some((w,result));}
            }
            let (weight,best)=best.unwrap();
            save_curve(output,codes,label,"hindsight_best",&best)?;
            let mut value=serde_json::to_value(best)?;value.as_object_mut().unwrap().remove("points");value["strategy"]=json!("hindsight_best");value["first_weight"]=json!(weight);strategies.push(value);
            scenarios.push(json!({"cost_scenario":label,"strategies":strategies}));
        }
        Ok(json!({"codes":codes,"first_date":dates[0],"last_date":dates.last(),"trading_days":dates.len(),"scenarios":scenarios}))
    }).collect::<Result<Vec<_>,Error>>()?;
    let report = json!({"protocol":protocol,"results":reports,"generated_at":chrono::Utc::now()});
    std::fs::write(
        output.join("report.json"),
        serde_json::to_vec_pretty(&report)?,
    )?;
    let mut csv = String::from(
        "pair,cost,strategy,total_return,annual_return,max_drawdown,sharpe,annual_traded_notional_over_nav,fees,fills,blocked_open_days,missing_pair_days\n",
    );
    for pair in report["results"].as_array().unwrap() {
        let pair_name = format!(
            "{}:{}",
            pair["codes"][0].as_str().unwrap(),
            pair["codes"][1].as_str().unwrap()
        );
        for scenario in pair["scenarios"].as_array().unwrap() {
            for s in scenario["strategies"].as_array().unwrap() {
                csv.push_str(&format!(
                    "{},{},{},{},{},{},{},{},{},{},{},{}\n",
                    pair_name,
                    scenario["cost_scenario"].as_str().unwrap(),
                    s["strategy"].as_str().unwrap(),
                    s["total_return"],
                    s["annual_return"],
                    s["max_drawdown"],
                    s["sharpe_zero_rf"],
                    s["annual_traded_notional_over_nav"],
                    s["fees"],
                    s["fills"],
                    s["blocked_open_days"],
                    s["missing_pair_days"]
                ));
            }
        }
    }
    std::fs::write(output.join("summary.csv"), csv)?;
    write_html(output, &report)?;
    println!(
        "Saved {} pair experiments to {}",
        pairs.len(),
        output.display()
    );
    Ok(())
}

fn write_html(output: &Path, report: &serde_json::Value) -> Result<(), Error> {
    let mut html = String::from(
        "<!doctype html><html lang=zh-CN><meta charset=utf-8><title>A 股通用组合回测</title><style>body{font:16px system-ui;max-width:1150px;margin:40px auto;padding:0 20px;color:#182432}table{border-collapse:collapse;width:100%;margin:20px 0 40px}th,td{padding:10px;border-bottom:1px solid #ddd;text-align:right}td:first-child,th:first-child{text-align:left}th{background:#edf2f7}p{line-height:1.7}.note{background:#fff5dc;padding:18px}a{color:#1260a0}</style><h1>A 股 Cover 通用组合探索</h1>",
    );
    html.push_str(&format!("<p>请求区间：{} 至 {}；初始资金：{:.0} 元。每日收盘更新专家财富，次日开盘按整手执行。专家网格 {} 个。</p>",report["protocol"]["start"].as_str().unwrap(),report["protocol"]["end"].as_str().unwrap(),report["protocol"]["initial_capital"].as_f64().unwrap(),report["protocol"]["grid_points"]));
    html.push_str("<p class=note>这是固定存续股票的探索性实验，存在幸存者偏差。复权因子通过合成份额近似总回报；缺报价日沿用旧估值并暂停整对调仓；开盘相对前收偏离 ≥4.5% 时保守暂停整对调仓。历史 ST、股息交收、精确涨跌停队列与容量未完整建模。配置税率全区间固定。零成本仍保留整手及执行约束，事后最优比例不能用作无前视信号。</p><p><a href=protocol.json>完整方法及参数</a> · <a href=summary.csv>汇总 CSV</a> · <a href=report.json>完整 JSON 与逐年收益</a> · <a href=input_prices.csv>输入价格</a></p>");
    for pair in report["results"].as_array().unwrap() {
        let a = pair["codes"][0].as_str().unwrap();
        let b = pair["codes"][1].as_str().unwrap();
        html.push_str(&format!("<h2>{a} + {b}</h2><p>{} 至 {}，{} 个交易日。目标比例受整手和现金约束，实际权重见每日 CSV。</p>",pair["first_date"].as_str().unwrap(),pair["last_date"].as_str().unwrap(),pair["trading_days"]));
        for scenario in pair["scenarios"].as_array().unwrap() {
            let cost = scenario["cost_scenario"].as_str().unwrap();
            let label = match cost {
                "zero_cost" => "零费用",
                "configured_cost" => "系统配置费用",
                _ => "双倍费用",
            };
            html.push_str(&format!("<h3>{label}</h3><table><tr><th>策略 / 每日净值</th><th>累计收益</th><th>年化收益</th><th>最大回撤</th><th>夏普（无风险=0）</th><th>成交笔数</th><th>缺报价 / 限制日</th></tr>"));
            for s in scenario["strategies"].as_array().unwrap() {
                let name = s["strategy"].as_str().unwrap();
                let label = match name {
                    "universal" => "通用组合",
                    "daily_50_50" => "每日 50/50",
                    "weekly_50_50" => "每 5 日 50/50",
                    "monthly_50_50" => "每 21 日 50/50",
                    "hold_50_50" => "50/50 买入持有",
                    "hold_first" => "仅持有第一只",
                    "hold_second" => "仅持有第二只",
                    _ => "事后最优每日比例（仅参照）",
                };
                html.push_str(&format!("<tr><td><a href='{a}_{b}_{cost}_{name}.csv'>{label}</a></td><td>{:.2}%</td><td>{:.2}%</td><td>{:.2}%</td><td>{:.2}</td><td>{}</td><td>{} / {}</td></tr>",s["total_return"].as_f64().unwrap()*100.0,s["annual_return"].as_f64().unwrap()*100.0,s["max_drawdown"].as_f64().unwrap()*100.0,s["sharpe_zero_rf"].as_f64().unwrap(),s["fills"],s["missing_pair_days"],s["blocked_open_days"]));
            }
            html.push_str("</table>");
        }
    }
    html.push_str("</html>");
    std::fs::write(output.join("report.html"), html)?;
    Ok(())
}

fn save_curve(
    output: &Path,
    codes: &[String; 2],
    scenario: &str,
    name: &str,
    result: &universal::Result,
) -> Result<(), Error> {
    let mut csv = String::from("date,nav,target_first,actual_first,cash\n");
    for p in &result.points {
        csv.push_str(&format!(
            "{},{:.8},{:.8},{:.8},{:.8}\n",
            p.date, p.nav, p.target_first, p.actual_first, p.cash
        ));
    }
    std::fs::write(
        output.join(format!(
            "{}_{}_{}_{}.csv",
            codes[0], codes[1], scenario, name
        )),
        csv,
    )?;
    Ok(())
}
