// Command execution functionality integrated from ai_bridge
// This replaces file-based communication with direct HTTP requests
// SPDX-License-Identifier: GPL-3.0-or-later

#pragma once

#include <string>

namespace ps5lm {

/**
 * Execute a command on the remote server
 * Integrated into PS5LM's architecture
 */
std::string executeCommand(const std::string &command);

/**
 * Check if the command server is ready
 */
bool isCommandServerReady();

/**
 * Initialize the command executor
 */
void init_command_executor();

}  // namespace ps5lm
