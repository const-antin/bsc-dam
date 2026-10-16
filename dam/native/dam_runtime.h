#pragma once
#include <cstdint>
#include <vector>
// Payloads are packed Bits, least-significant 32-bit word first. Width zero
// carries an empty vector but is still a valid FIFO token.
using DamBits = std::vector<uint32_t>;
void dam_begin_cycle(void* sim = nullptr);
void dam_end_cycle();
void dam_rule_fired();
bool dam_state_tracing();
bool dam_memory_needed(const char* name);
void dam_reg_write(const char* name, const DamBits& value, unsigned long long physical_time);
bool dam_remote(const char* name);
bool dam_ready(const char* name);
bool dam_valid(const char* name);
const DamBits& dam_front(const char* name);
void dam_enqueue(const char* name, const DamBits& value);
void dam_dequeue(const char* name, const DamBits& value);

void dam_reset_fifo(const char* name);
