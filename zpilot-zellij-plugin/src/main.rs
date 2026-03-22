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
    /// Last known pane info keyed by (pane_id, is_plugin)
    panes: BTreeMap<(u32, bool), PaneInfoReport>,
    /// Whether we've completed initial setup
    initialized: bool,
    /// Plugin's own pane ID
    own_pane_id: Option<u32>,
    /// Pending commands to write to panes (pane_id -> commands)
    pending_writes: Vec<PaneWrite>,
    /// Timer tick counter for diagnostics
    tick_count: u64,
}

#[derive(Clone, Debug, Serialize)]
struct PaneInfoReport {
    pane_id: u32,
    is_plugin: bool,
    title: String,
    is_focused: bool,
    is_floating: bool,
    exit_status: Option<i32>,
}

#[derive(Clone, Debug, Deserialize)]
struct PaneWrite {
    pane_id: u32,
    #[serde(default)]
    is_plugin: bool,
    text: String,
}

#[derive(Serialize)]
struct StatusReport {
    session_name: String,
    panes: Vec<PaneInfoReport>,
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

        // Request permissions needed for our operations
        request_permission(&[
            PermissionType::ReadApplicationState,
            PermissionType::ChangeApplicationState,
            PermissionType::WriteToStdin,
            PermissionType::WebAccess,
        ]);

        // Subscribe to relevant events (including PermissionRequestResult and WebRequestResult)
        subscribe(&[
            EventType::PaneUpdate,
            EventType::TabUpdate,
            EventType::SessionUpdate,
            EventType::Timer,
            EventType::PermissionRequestResult,
            EventType::WebRequestResult,
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
                eprintln!("[zpilot] PaneUpdate received!");
                self.handle_pane_update(pane_manifest);
                should_render = true;
            }
            Event::TabUpdate(tabs) => {
                self.handle_tab_update(tabs);
            }
            Event::SessionUpdate(..) => {
                // Session list changed — could report to daemon
            }
            Event::PermissionRequestResult(status) => {
                eprintln!("[zpilot] Permission request result: {:?}", status);
            }
            Event::Timer(_elapsed) => {
                self.tick_count += 1;
                self.report_status();
                self.poll_commands();
                self.process_pending_writes();
                // Re-arm timer
                set_timeout(POLL_INTERVAL_SECS);
            }
            Event::WebRequestResult(_status, _headers, body, context) => {
                // Handle responses from our HTTP requests
                let req_type = context.get("type").map(|s| s.as_str()).unwrap_or("");
                match req_type {
                    "poll_commands" => {
                        if let Ok(body_str) = std::str::from_utf8(&body) {
                            self.handle_command_response(body_str);
                            // Execute writes immediately (don't wait for next timer tick)
                            self.process_pending_writes();
                        }
                    }
                    _ => {} // status_report responses are fire-and-forget
                }
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
                let info = PaneInfoReport {
                    pane_id: pane.id,
                    is_plugin: pane.is_plugin,
                    title: pane.title.clone(),
                    is_focused: pane.is_focused,
                    is_floating: pane.is_floating,
                    exit_status: pane.exit_status,
                };
                self.panes.insert((pane.id, pane.is_plugin), info);
            }
        }
    }

    fn handle_tab_update(&mut self, _tabs: Vec<TabInfo>) {
        // Could track tab names for richer reporting
    }

    fn report_status(&self) {
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
            let pane_id = if pw.is_plugin {
                PaneId::Plugin(pw.pane_id)
            } else {
                PaneId::Terminal(pw.pane_id)
            };
            write_to_pane_id(pw.text.into_bytes(), pane_id);
        }
    }

    fn poll_commands(&self) {
        let url = format!("{}/api/plugin-commands", self.daemon_url);
        let headers = BTreeMap::new();

        web_request(
            &url,
            HttpVerb::Get,
            headers,
            vec![],
            vec![("type".to_string(), "poll_commands".to_string())]
                .into_iter()
                .collect(),
        );
    }

    fn handle_command_response(&mut self, body: &str) {
        #[derive(Deserialize)]
        struct CommandsResponse {
            commands: Vec<PaneWrite>,
        }

        match serde_json::from_str::<CommandsResponse>(body) {
            Ok(resp) => {
                if !resp.commands.is_empty() {
                    eprintln!("[zpilot] Received {} commands", resp.commands.len());
                    self.pending_writes.extend(resp.commands);
                }
            }
            Err(e) => {
                eprintln!("[zpilot] Failed to parse commands: {}", e);
            }
        }
    }
}
