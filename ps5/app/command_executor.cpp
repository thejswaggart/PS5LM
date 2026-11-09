// Command execution functionality integrated from ai_bridge
// This replaces file-based communication with direct HTTP requests
// SPDX-License-Identifier: GPL-3.0-or-later

#include "command_executor.hpp"

#include "log.hpp"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

#include <cstdlib>
#include <cstring>
#include <string>

namespace ps5lm {

namespace {

// The command server. A numeric address: the platform's resolver always fails.
constexpr const char * kServerHost = "192.168.1.126";
constexpr int kServerPort = 8080;
constexpr const char * kServerPath = "/run";
constexpr int kTimeoutSeconds = 10;
constexpr size_t kMaxResponse = 512 * 1024;

std::string json_escape(const std::string & input) {
    std::string out;
    out.reserve(input.size() + 16);
    for (const char c : input) {
        switch (c) {
            case '\\': out += "\\\\"; break;
            case '"':  out += "\\\""; break;
            case '\n': out += "\\n"; break;
            case '\t': out += "\\t"; break;
            case '\r': out += "\\r"; break;
            case '\b': out += "\\b"; break;
            case '\f': out += "\\f"; break;
            default:
                if ((unsigned char) c < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof(buf), "\\u%04x", (unsigned) (unsigned char) c);
                    out += buf;
                } else {
                    out += c;
                }
        }
    }
    return out;
}

// POSTs body as JSON and puts the response body in response. False on any failure.
bool post_json(const char * host, int port, const char * path, const std::string & body, std::string & response) {
    sockaddr_in addr = {};
    addr.sin_family = AF_INET;
    addr.sin_port = htons((uint16_t) port);
    if (inet_pton(AF_INET, host, &addr.sin_addr) != 1) {
        log_line("command: bad server address %s\n", host);
        return false;
    }
    const int sock = socket(AF_INET, SOCK_STREAM, 0);
    if (sock < 0) {
        log_line("command: socket() failed\n");
        return false;
    }
    // Do not hang the app's main loop on a server that never answers.
    timeval tv = {};
    tv.tv_sec = kTimeoutSeconds;
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
    if (connect(sock, (const sockaddr *) &addr, sizeof(addr)) < 0) {
        log_line("command: connect to %s:%d failed\n", host, port);
        close(sock);
        return false;
    }

    std::string request = std::string("POST ") + path + " HTTP/1.1\r\n"
        "Host: " + host + ":" + std::to_string(port) + "\r\n"
        "Content-Type: application/json\r\n"
        "Content-Length: " + std::to_string(body.size()) + "\r\n"
        "Connection: close\r\n\r\n" + body;
    for (size_t sent = 0; sent < request.size();) {
        const ssize_t n = send(sock, request.data() + sent, request.size() - sent, 0);
        if (n <= 0) {
            log_line("command: send() failed\n");
            close(sock);
            return false;
        }
        sent += (size_t) n;
    }

    std::string raw;
    char buf[4096];
    for (;;) {
        const ssize_t n = recv(sock, buf, sizeof(buf), 0);
        if (n < 0) {
            log_line("command: recv() failed\n");
            close(sock);
            return false;
        }
        if (n == 0 || raw.size() + (size_t) n > kMaxResponse) {
            if (n > 0) raw.append(buf, (size_t) n);
            break;
        }
        raw.append(buf, (size_t) n);
    }
    close(sock);

    const size_t header_end = raw.find("\r\n\r\n");
    if (header_end == std::string::npos) {
        log_line("command: no response body (got %zu bytes)\n", raw.size());
        return false;
    }
    response = raw.substr(header_end + 4);
    return true;
}

// The value after "key": in a flat JSON object; skips whitespace after the colon.
size_t find_value(const std::string & json, const char * key) {
    const std::string needle = std::string("\"") + key + "\"";
    size_t pos = json.find(needle);
    if (pos == std::string::npos) return pos;
    pos = json.find(':', pos + needle.size());
    if (pos == std::string::npos) return pos;
    ++pos;
    while (pos < json.size() && (json[pos] == ' ' || json[pos] == '\t' || json[pos] == '\n' || json[pos] == '\r')) ++pos;
    return pos < json.size() ? pos : std::string::npos;
}

// A JSON string value, unescaped (\uXXXX kept as-is beyond ASCII).
bool json_string(const std::string & json, const char * key, std::string & out) {
    size_t pos = find_value(json, key);
    if (pos == std::string::npos || json[pos] != '"') return false;
    out.clear();
    for (++pos; pos < json.size(); ++pos) {
        const char c = json[pos];
        if (c == '"') return true;
        if (c != '\\' || pos + 1 >= json.size()) {
            out += c;
            continue;
        }
        const char e = json[++pos];
        switch (e) {
            case 'n': out += '\n'; break;
            case 't': out += '\t'; break;
            case 'r': out += '\r'; break;
            case 'b': out += '\b'; break;
            case 'f': out += '\f'; break;
            case 'u':
                if (pos + 4 < json.size()) {
                    const long code = std::strtol(json.substr(pos + 1, 4).c_str(), nullptr, 16);
                    out += code < 0x80 ? (char) code : '?';
                    pos += 4;
                }
                break;
            default: out += e; break;  // \" \\ \/
        }
    }
    return false;  // unterminated
}

bool send_command_to_server(const std::string & command, std::string & stdout_out, std::string & stderr_out,
                            int & exit_code) {
    const std::string body = "{\"command\":\"" + json_escape(command) + "\"}";
    log_line("command: sending %s\n", command.c_str());

    std::string response;
    if (!post_json(kServerHost, kServerPort, kServerPath, body, response)) {
        return false;
    }
    if (!json_string(response, "stdout", stdout_out)) {
        log_line("command: no stdout in response\n");
        return false;
    }
    if (!json_string(response, "stderr", stderr_out)) {
        log_line("command: no stderr in response\n");
        return false;
    }
    const size_t code = find_value(response, "exit_code");
    if (code == std::string::npos) {
        log_line("command: no exit_code in response\n");
        return false;
    }
    exit_code = (int) std::strtol(response.c_str() + code, nullptr, 10);
    log_line("command: exit_code=%d\n", exit_code);
    return true;
}

}  // namespace

std::string executeCommand(const std::string & command) {
    std::string stdout_out, stderr_out;
    int exit_code = -1;
    if (!send_command_to_server(command, stdout_out, stderr_out, exit_code)) {
        return "ERROR: Command execution failed";
    }
    return "EXIT_CODE=" + std::to_string(exit_code) + "\nSTDOUT=" + stdout_out + "\nSTDERR=" + stderr_out + "\n";
}

bool isCommandServerReady() {
    return true;  // not checked yet
}

void init_command_executor() {
    // Nothing to set up yet.
}

}  // namespace ps5lm
