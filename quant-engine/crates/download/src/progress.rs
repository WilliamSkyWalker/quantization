//! Progress tracking for downloads.

use indicatif::{ProgressBar, ProgressStyle};

/// Create a progress bar for ticker-level downloads.
pub fn ticker_progress(total: u64, label: &str) -> ProgressBar {
    let pb = ProgressBar::new(total);
    pb.set_style(
        ProgressStyle::default_bar()
            .template(&format!("{{spinner:.green}} {label} [{{bar:40.cyan/blue}}] {{pos}}/{{len}} ({{eta}})"))
            .unwrap_or_else(|_| ProgressStyle::default_bar())
            .progress_chars("#>-"),
    );
    pb
}
