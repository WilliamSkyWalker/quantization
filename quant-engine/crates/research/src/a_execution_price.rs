//! Research-only entry-price assumptions. Full-day ranges are not intraday signals.
use crate::data::Bar;
use serde::Serialize;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum EntryPriceModel {
    Open,
    DailyRangeTwoThirds,
}

impl EntryPriceModel {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Open => "open",
            Self::DailyRangeTwoThirds => "daily-range-two-thirds",
        }
    }

    pub fn description(self) -> &'static str {
        match self {
            Self::Open => "observation date + 2 SSE trading days, open",
            Self::DailyRangeTwoThirds => {
                "observation date + 2 SSE trading days, low + (high - low) * 2/3; full-day OHLC fill proxy"
            }
        }
    }

    pub fn uses_full_day_range(self) -> bool {
        self == Self::DailyRangeTwoThirds
    }

    pub fn adjusted_entry(self, bar: &Bar) -> f64 {
        if !bar.vol.is_finite()
            || bar.vol <= 0.0
            || !bar.adj_factor.is_finite()
            || bar.adj_factor <= 0.0
        {
            return f64::NAN;
        }
        let price = match self {
            Self::Open => bar.open,
            Self::DailyRangeTwoThirds => {
                if !bar.low.is_finite()
                    || !bar.high.is_finite()
                    || bar.low <= 0.0
                    || bar.high < bar.low
                {
                    return f64::NAN;
                }
                bar.low + (bar.high - bar.low) * (2.0 / 3.0)
            }
        };
        let adjusted = price * bar.adj_factor;
        if price > 0.0 && adjusted.is_finite() && adjusted > 0.0 {
            adjusted
        } else {
            f64::NAN
        }
    }
}

impl std::str::FromStr for EntryPriceModel {
    type Err = String;
    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value {
            "open" => Ok(Self::Open),
            "daily-range-two-thirds" => Ok(Self::DailyRangeTwoThirds),
            _ => Err("entry-price must be open or daily-range-two-thirds".into()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn two_thirds_means_from_low_towards_high_and_uses_adjustment() {
        let bar = Bar {
            low: 9.0,
            high: 12.0,
            open: 10.0,
            vol: 100.0,
            adj_factor: 2.0,
            ..Bar::default()
        };
        assert_eq!(
            EntryPriceModel::DailyRangeTwoThirds.adjusted_entry(&bar),
            22.0
        );
        assert_eq!(EntryPriceModel::Open.adjusted_entry(&bar), 20.0);
        let flat = Bar {
            low: 9.0,
            high: 9.0,
            ..bar
        };
        assert_eq!(
            EntryPriceModel::DailyRangeTwoThirds.adjusted_entry(&flat),
            18.0
        );
    }
    #[test]
    fn bad_range_is_missing_without_fallback_to_open() {
        let mut bar = Bar {
            low: 12.0,
            high: 9.0,
            open: 10.0,
            vol: 100.0,
            adj_factor: 1.0,
            ..Bar::default()
        };
        assert!(
            EntryPriceModel::DailyRangeTwoThirds
                .adjusted_entry(&bar)
                .is_nan()
        );
        assert_eq!(EntryPriceModel::Open.adjusted_entry(&bar), 10.0);
        bar.high = f64::NAN;
        assert!(
            EntryPriceModel::DailyRangeTwoThirds
                .adjusted_entry(&bar)
                .is_nan()
        );
        bar.low = 9.0;
        bar.high = 12.0;
        bar.vol = 0.0;
        assert!(
            EntryPriceModel::DailyRangeTwoThirds
                .adjusted_entry(&bar)
                .is_nan()
        );
    }
}
