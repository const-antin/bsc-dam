#pragma once
#include "dam_runtime.h"
#include "bs_wide_data.h"
#include "bs_module.h"
#include <string>

inline std::string dam_full_name(const Module* mod) {
    std::string name = mod->inst_name;
    for (auto p = mod->parent; p && p->parent; p = p->parent)
        name = std::string(p->inst_name) + "." + name;
    return name;
}

inline void dam_mask(DamBits& words, unsigned bits) {
    if (bits % 32) words.back() &= (uint32_t(1) << (bits % 32)) - 1;
}
template<typename T> DamBits dam_pack(const T& value, unsigned bits) {
    DamBits words((uint64_t(bits) + 31) / 32);
    // Reading no data for a zero-width token also avoids observing dummyval.
    for (size_t i = 0; i < words.size(); ++i)
        words[i] = uint32_t(uint64_t(value) >> (32 * i));
    dam_mask(words, bits);
    return words;
}
inline DamBits dam_pack(const WideData& value, unsigned bits) {
    DamBits words((uint64_t(bits) + 31) / 32);
    for (size_t i = 0; i < words.size(); ++i) words[i] = value[i];
    dam_mask(words, bits);
    return words;
}
template<typename T> void dam_unpack(const DamBits& words, unsigned, T& value) {
    uint64_t packed = 0;
    for (size_t i = 0; i < words.size(); ++i) packed |= uint64_t(words[i]) << (32 * i);
    value = T(packed);
}
inline void dam_unpack(const DamBits& words, unsigned, WideData& value) {
    for (size_t i = 0; i < words.size(); ++i) value[i] = words[i];
}
