// What the user can change on the Settings page, kept in
// /data/PS5LM/settings.json. Unknown or missing keys keep their defaults, so
// an old file still loads after a new setting is added.
//
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

#include <cstdint>
#include <string>

namespace ps5lm {

struct Settings {
    bool        auto_load = false;  // load default_model at launch
    std::string default_model;      // a path from the library, or empty
    uint32_t    ctx_cap   = 65536;  // the planner's longest context
    std::string kv_type   = "auto"; // "auto", "f16", "q8_0" or "q4_0"
    bool        sounds    = true;
    bool        tools     = true;   // the browser chat's tools, working in the scratch folder
    // Its file tools (read_file, write_file, ...); off leaves the handoff note,
    // context_status and remote_command, for a model that mixes up this PS5's paths
    // with the command server's.
    bool        file_tools = true;
    // The browser chat's remote_command tool (patches/0010): the command server as
    // "host:port", a numeric address (empty: no tool), and the token it checks.
    std::string remote_command;
    std::string remote_command_token;
    bool        introspect = false;  // POST /v1/introspect: raw logits and activations (off by default)
    uint32_t    scratch_limit_gib = 2;  // warn when the scratch folder grows past this
    std::string download_dir = "/data/PS5LM/models";  // where the market saves models
    bool        report_kernel_log = false;  // the debug report includes the kernel log
    bool        report_explained  = false;  // what a debug report holds was shown before the first

    bool operator==(const Settings &) const = default;
};

Settings load_settings(const char * path);
bool     save_settings(const char * path, const Settings & s);

}  // namespace ps5lm
