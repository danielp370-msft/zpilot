//! zpilot Zellij Plugin
//!
//! Bridges Zellij's internal events to the zpilot daemon via HTTP.
//! Subscribes to pane/tab updates and reports session state in real-time.
//! Also provides a command input mechanism for headless pane injection.

use serde::{Deserialize, Serialize};
use zellij_tile::prelude::*;

use std::collections::BTreeMap;

/// Plugin state maintained across events.
#[derive(Default)]
struct ZpilotPlugin {
    /// zpilot daemon HTTP endpoint (configured via plugin config)
    daemon_url: String,
    /// Auth token for daemon API
    auth_token: String,
    /// Last known pane info keyed by pane_id
    panes: BTreeMap<u32, PaneInfo>,
    /// Whether we've completed initial setup
    initialized: bool,
    /// Plugin's own pane ID
    own_pane_id: Option<u32>,
    /// Pending commands to write to panes (pane_id -> commands)
    pending_writes: Vec<PaneWrite>,
}

#[derive(Clone, Debug, Serialize)]
struct PaneInfo {
    pane_id: u32,
    title: String,
    is_focused: bool,
    exit_status: Option<i32>,
}

#[derive(Clone, Debug, Deserialize)]
struct PaneWrite {
    pane_id: u32,
    text: String,
}

#[derive(Serialize)]
struct StatusReport {
    session_name: String,
    panes: Vec<PaneInfo>,
    plugin_version: &'static str,
}

const PLUGIN_VERSION: &str = env!("CARGO_PKG_VERSION");
const DEFAULT_DAEMON_URL: &str = "http://127.0.0.1:8095";
const POLL_INTERVAL_SECS: f64 = 2.0;

register_plugin!(ZpilotPlugin);

impl ZellijPlugin for ZpilotPlugin {
    fn load(&mut self, configuration: BTreeMap<String, String>) {
        // Read config passed via Zellij plugin configuration
        self.daemon_url = configuration
            .get("daemon_url")
            .cloned()
            .unwrap_or_else(|| DEFAULT_DAEMON_URL.to_string());

        self.auth_token = configuration
            .get("auth_token")
            .cloned()
            .unwrap_or_default();

        // Subscribe to relevant events
        subscribe(&[
            EventType::PaneUpdate,
            EventType::TabUpdate,
            EventType::SessionUpdate,
            EventType::Timer,
        ]);

        // Start periodic timer for status reporting
        set_timeout(POLL_INTERVAL_SECS);

        self.initialized = true;
        eprintln!("[zpilot] Plugin loaded, daemon: {}", self.daemon_url);
    }

    fn update(&mut self, event: Event) -> bool {
        let mut should_render = false;

        match event {
            Event::PaneUpdate(pane_manifest) => {
                self.handle_pane_update(pane_manifest);
                should_render = true;
            }
            Event::TabUpdate(tabs) => {
                self.handle_tab_update(tabs);
            }
            Event::SessionUpdate(..) => {
                // Session list changed — could report to daemon
            }
            Event::Timer(_elapsed) => {
                self.report_status();
                self.process_pending_writes();
                // Re-arm timer
                set_timeout(POLL_INTERVAL_SECS);
            }
            _ => {}
        }

        should_render
    }

    fn render(&mut self, _rows: usize, _cols: usize) {
        // Minimal status bar rendering
        let pane_count = self.panes.len();
        let focused = self
            .panes
            .values()
            .filter(|p| p.is_focused)
            .count();

        println!(
            "zpilot v{} | {} panes ({} focused) | daemon: {}",
            PLUGIN_VERSION,
            pane_count,
            focused,
            if self.initialized { "connected" } else { "..." }
        );
    }
}

impl ZpilotPlugin {
    fn handle_pane_update(&mut self, manifest: PaneManifest) {
        self.panes.clear();

        for (_tab_idx, pane_list) in &manifest.panes {
            for pane in pane_list {
                let info = PaneInfo {
                    pane_id: pane.id,
                    title: pane.title.clone(),
                    is_focused: pane.is_focused,
                    exit_status: pane.exit_status,
                };
                self.panes.insert(pane.id, info);
            }
        }
    }

    fn handle_tab_update(&mut self, _tabs: Vec<TabInfo>) {
        // Could track tab names for richer reporting
    }

    fn report_status(&self) {
        if self.panes.is_empty() {
            return;
        }

        let report = StatusReport {
            session_name: String::from("zellij"), // Zellij doesn't expose session name to plugins
            panes: self.panes.values().cloned().collect(),
            plugin_version: PLUGIN_VERSION,
        };

        match serde_json::to_string(&report) {
            Ok(json) => {
                // Use Zellij's web_request to POST to the daemon
                let url = format!("{}/api/plugin-status", self.daemon_url);
                let mut headers = BTreeMap::new();
                headers.insert("Content-Type".to_string(), "application/json".to_string());
                if !self.auth_token.is_empty() {
                    headers.insert(
                        "Authorization".to_string(),
                        format!("Bearer {}", self.auth_token),
                    );
                }

                // Zellij web_request is fire-and-forget from plugin perspective
                // Response comes back as Event::WebRequestResult
                web_request(
                    &url,
                    HttpVerb::Post,
                    headers,
                    json.into_bytes(),
                    // context for matching response
                    vec![("type".to_string(), "status_report".to_string())]
                        .into_iter()
                        .collect(),
                );
            }
            Err(e) => {
                eprintln!("[zpilot] Failed to serialize status: {}", e);
            }
        }
    }

    fn process_pending_writes(&mut self) {
        for pw in self.pending_writes.drain(..) {
            write_to_pane_id(pw.text.into_bytes(), PaneId::Terminal(pw.pane_id));
        }
    }
}
