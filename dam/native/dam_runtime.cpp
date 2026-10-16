#include "dam_runtime.h"
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#ifdef DAM_WITH_SKIP_KERNEL
extern "C" bool bk_dam_skip_cycles(void*, unsigned long long);
extern "C" bool bk_dam_check_clock(void*, unsigned long long);
#else
// The ordinary bridge still links against an unmodified stock kernel.
static bool bk_dam_skip_cycles(void*, unsigned long long) { return false; }
static bool bk_dam_check_clock(void*, unsigned long long) { return false; }
#endif

namespace {
struct Channel { std::string name; uint32_t width=0, ready=0, valid=0, enq=0, deq=0; DamBits data, out; };
std::vector<Channel> channels;
uint32_t cycle=0, cycles=0;
uint32_t skipped=0;
void* sim_handle=nullptr;
bool fired=false;
int fd=-1;
// Reuse complete wire frames. A word is a serialization unit, not an I/O
// operation: one blocking exchange per clock is already expensive enough.
std::vector<unsigned char> request, response;
size_t read_offset=0, write_offset=0;
FILE* trace=nullptr;
FILE* state_trace=nullptr;
bool initialized=false, sparse=false, batched=false, batch_active=false;
uint32_t batch_until=0;
size_t max_request=0;
constexpr uint32_t magic=0x44414d33;
[[noreturn]] void fail(const char* message) {
    std::fprintf(stderr, "DAM bridge: %s (cycle %u)\n", message, cycle);
    std::_Exit(2);
}
void io(unsigned char* bytes, size_t count, bool writing) {
    while (count) {
        ssize_t n=writing ? write(fd,bytes,count) : read(fd,bytes,count);
        if (n<0 && errno==EINTR) continue;
        if (n<=0) fail("transport closed");
        bytes+=n; count-=size_t(n);
    }
}
uint32_t read_word() {
    if (read_offset+4>request.size()) fail("request frame overrun");
    const auto* b=request.data()+read_offset; read_offset+=4;
    return uint32_t(b[0]) | uint32_t(b[1])<<8 | uint32_t(b[2])<<16 | uint32_t(b[3])<<24;
}
void write_word(uint32_t x) {
    if (write_offset+4>response.size()) response.resize(write_offset+4);
    auto* b=response.data()+write_offset; write_offset+=4;
    for (int i=0;i<4;++i) b[i]=(x>>(8*i))&255;
}
Channel* channel(const char* name) {
    for (auto& c:channels) if (c.name==name) return &c;
    return nullptr;
}
void log_value(const Channel& c, const char* action, const DamBits& value) {
    if(value.size()!=c.data.size()) fail("payload length mismatch");
    if(c.width%32 && (value.back() >> (c.width%32))) fail("noncanonical payload");
    if (!trace) return;
    std::fprintf(trace,"%u %s %s 0x",cycle,c.name.c_str(),action);
    if(value.empty()) std::fputc('0',trace);
    for(auto it=value.rbegin(); it!=value.rend(); ++it) std::fprintf(trace,"%08x",*it);
    std::fputc('\n',trace);
}

void init() {
    if (initialized) return;
    initialized=true;
    const char* names=std::getenv("DAM_CHANNELS");
    const char* limit=std::getenv("DAM_CYCLES");
    const char* file=std::getenv("DAM_TRACE");
    if (!names || !limit || !file) fail("missing runtime configuration");
    cycles=uint32_t(std::strtoul(limit,nullptr,10));
    if (!cycles) fail("zero cycle limit");
    std::string s(names);
    for (size_t from=0; from<s.size();) {
        auto end=s.find(',',from); if (end==std::string::npos) end=s.size();
        auto entry=s.substr(from,end-from);
        auto colon=entry.rfind(':');
        if(colon==std::string::npos) fail("channel requires name:width");
        char* tail=nullptr;
        auto width=std::strtoull(entry.c_str()+colon+1,&tail,10);
        if(!tail || *tail || tail==entry.c_str()+colon+1 || width>UINT32_MAX) fail("invalid payload width");
        Channel c; c.name=entry.substr(0,colon); c.width=uint32_t(width);
        c.data.resize((width+31)/32); c.out.resize(c.data.size());
        if(c.name.empty() || channel(c.name.c_str())) fail("invalid channel name");
        channels.push_back(std::move(c)); from=end+1;
    }
    const char* transport=std::getenv("DAM_TRANSPORT");
    batched=transport && std::strcmp(transport,"batched")==0;
    sparse=batched || (transport && std::strcmp(transport,"sparse")==0);
    size_t channel_words=0;
    for (const auto& c:channels) channel_words+=3+c.data.size();
    max_request=4*(3+unsigned(batched)+channel_words);
    request.resize(max_request);
    response.resize(4*(6+channel_words));
    if (std::strcmp(file,"/dev/null") != 0) {
        trace=std::fopen(file,"w"); if (!trace) fail("cannot open trace");
    }
    if(const char* state_file=std::getenv("DAM_STATE_TRACE")) {
        if(*state_file) { state_trace=std::fopen(state_file,"w"); if(!state_trace) fail("cannot open state trace"); }
    }
    if (const char* socket=std::getenv("DAM_SOCKET")) {
        fd=::socket(AF_UNIX,SOCK_STREAM,0); if(fd<0) fail("socket");
        sockaddr_un addr{}; addr.sun_family=AF_UNIX;
        if(std::strlen(socket)>=sizeof(addr.sun_path)) fail("socket path too long");
        std::strcpy(addr.sun_path,socket);
        if(connect(fd,reinterpret_cast<sockaddr*>(&addr),sizeof(addr))) fail("connect");
    }
}
}
void dam_begin_cycle(void* sim) {
    init();
    sim_handle=sim; fired=false;
    for(auto& c:channels) c.enq=c.deq=0;
    if(fd<0) return;
    // No clocks are omitted. The host has proved this boundary snapshot
    // stable until batch_until; the first committed transfer ends the batch.
    if(batch_active) return;
    skipped=0;
    if(sparse) {
        unsigned char header[4]; io(header,4,false);
        size_t bytes=uint32_t(header[0]) | uint32_t(header[1])<<8 |
                     uint32_t(header[2])<<16 | uint32_t(header[3])<<24;
        if(bytes<4*(3+unsigned(batched)+3*channels.size()) || bytes>max_request || bytes%4)
            fail("invalid request length");
        request.resize(bytes);
    }
    io(request.data(),request.size(),false);
    read_offset=0;
    if(read_word()!=(batched ? magic+2 : sparse ? magic+1 : magic)) fail("bad request magic");
    uint32_t target=read_word();
    if(target<cycle || target>=cycles || read_word()!=channels.size()) fail("bad request header");
    batch_until=batched ? read_word() : target;
    if(batch_until<target || batch_until>=cycles) fail("invalid batch horizon");
    skipped=target-cycle;
    if(skipped && !bk_dam_skip_cycles(sim,skipped)) fail("unsafe kernel clock skip");
    cycle=target;
    if(skipped && !bk_dam_check_clock(sim,cycle)) fail("kernel time or edge count mismatch");
    for(auto& c:channels) {
        if(read_word()!=c.width) fail("payload width mismatch");
        c.ready=read_word(); c.valid=read_word();
        if(c.ready>1 || c.valid>1) fail("invalid handshake");
        if(!sparse || c.valid) {
            for(auto& w:c.data) w=read_word();
            if(c.width%32 && (c.data.back() >> (c.width%32))) fail("noncanonical payload");
        }
        c.enq=c.deq=0;
    }
    if(read_offset!=request.size()) fail("request frame length mismatch");
    batch_active=batched;
}
void dam_end_cycle() {
    // Report committed transfers after reset ticks have had a chance to
    // cancel writes from the initial reset edge, just as local FIFOs do.
    for(const auto& c:channels) {
        if(c.enq) log_value(c,"enq",c.out);
        if(c.deq) log_value(c,"deq",c.data);
    }
    bool transfer=false;
    for(const auto& c:channels) transfer=transfer || c.enq || c.deq;
    if(fd>=0 && (!batch_active || transfer || cycle>=batch_until)) {
        write_offset=0;
        if(sparse) write_word(0); // byte length, filled after encoding
        write_word(batched ? magic+2 : sparse ? magic+1 : magic); write_word(cycle); write_word(uint32_t(channels.size()));
        write_word(bk_dam_skip_cycles(sim_handle,0));
        write_word(skipped); write_word(fired);
        for(const auto& c:channels) {
            write_word(c.width); write_word(c.enq); write_word(c.deq);
            if(!sparse || c.enq) for(auto w:c.out) write_word(w);
        }
        if(sparse) {
            uint32_t length=uint32_t(write_offset-4);
            for(int i=0;i<4;++i) response[i]=(length>>(8*i))&255;
        } else if(write_offset!=response.size()) fail("response frame length mismatch");
        io(response.data(),write_offset,true);
        batch_active=false;
    }
    if(++cycle==cycles) {
        std::fflush(nullptr);
        if(fd>=0) close(fd);
        std::exit(0);
    }
}
void dam_rule_fired() { fired=true; }
bool dam_memory_needed(const char* name) {
    // Called during model construction, before dam_begin_cycle initializes
    // the bridge. Only the native compiler's proven unowned memories appear.
    static const std::string omitted=std::string(",")+
        (std::getenv("DAM_UNUSED_MEMORIES") ? std::getenv("DAM_UNUSED_MEMORIES") : "")+",";
    return omitted.find(std::string(",")+name+",")==std::string::npos;
}
bool dam_state_tracing() { return state_trace!=nullptr; }
void dam_reg_write(const char* name,const DamBits& value,unsigned long long physical_time) {
    if(!state_trace) return;
    std::fprintf(state_trace,"%u %llu %s 0x",cycle,physical_time,name);
    if(value.empty()) std::fputc('0',state_trace);
    for(auto it=value.rbegin();it!=value.rend();++it) std::fprintf(state_trace,"%08x",*it);
    std::fputc('\n',state_trace);
}
bool dam_remote(const char* name) { return initialized && fd>=0 && channel(name); }
bool dam_ready(const char* name) { return channel(name)->ready; }
bool dam_valid(const char* name) { return channel(name)->valid; }
const DamBits& dam_front(const char* name) { return channel(name)->data; }
void dam_enqueue(const char* name,const DamBits& value) {
    auto c=channel(name); if(!c) return;
    if(fd>=0) {
        if(!c->ready || c->enq) fail("illegal enqueue");
    }
    c->enq=1; c->out=value;
}
void dam_dequeue(const char* name,const DamBits& value) {
    auto c=channel(name); if(!c) return;
    if(fd>=0) {
        if(!c->valid || c->deq || c->data!=value) fail("illegal dequeue");
    }
    c->deq=1; c->data=value;
}

void dam_reset_fifo(const char* name) {
    if(auto c=channel(name)) c->enq=c->deq=0;
}
