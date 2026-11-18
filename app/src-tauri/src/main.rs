// No console window next to the app in a release build on Windows.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    vault_ingest_app::run()
}
