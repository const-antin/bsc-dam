// Development-only exact comparison of chronological simulation traces.
// Ignore ordering within a clock, as the existing external-sort checker does.
#include <algorithm>
#include <charconv>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <memory>
#include <queue>
#include <stdexcept>
#include <string>
#include <vector>

struct Cursor {
    std::ifstream file;
    std::string path, line;
    uint64_t cycle=0;
    bool present=false, started=false;
    explicit Cursor(const char* p): file(p), path(p) {
        if(!file) throw std::runtime_error("cannot open "+path);
        advance();
    }
    void advance() {
        if(!std::getline(file,line)) {
            if(!file.eof()) throw std::runtime_error("read failed: "+path);
            present=false; return;
        }
        auto space=line.find(' ');
        uint64_t next=0;
        auto parsed=std::from_chars(line.data(),line.data()+line.size(),next);
        if(space==std::string::npos || parsed.ec!=std::errc{} ||
           parsed.ptr!=line.data()+space)
            throw std::runtime_error("invalid cycle: "+path);
        if(started && next<cycle) throw std::runtime_error("nonchronological trace: "+path);
        cycle=next;present=true;started=true;
    }
    void group(uint64_t target,std::vector<std::string>& lines) {
        while(present && cycle==target) { lines.push_back(line);advance(); }
    }
};
int main(int argc,char** argv) {
    try {
        if(argc<3) throw std::runtime_error("usage: compare-traces EXPECTED OBSERVED...");
        Cursor expected(argv[1]);
        std::vector<std::unique_ptr<Cursor>> files;
        auto later=[](const Cursor* a,const Cursor* b) {return a->cycle>b->cycle;};
        std::priority_queue<Cursor*,std::vector<Cursor*>,decltype(later)> heads(later);
        for(int i=2;i<argc;++i) {
            files.emplace_back(new Cursor(argv[i]));
            if(files.back()->present) heads.push(files.back().get());
        }
        uint64_t events=0,cycles=0;
        std::vector<std::string> want,got;
        while(expected.present || !heads.empty()) {
            if(!expected.present || heads.empty() || expected.cycle!=heads.top()->cycle)
                throw std::runtime_error("missing or extra cycle near "+std::to_string(expected.cycle));
            auto cycle=expected.cycle;want.clear();got.clear();
            expected.group(cycle,want);
            while(!heads.empty() && heads.top()->cycle==cycle) {
                auto cursor=heads.top();heads.pop();cursor->group(cycle,got);
                if(cursor->present) heads.push(cursor);
            }
            std::sort(want.begin(),want.end());std::sort(got.begin(),got.end());
            if(want!=got) throw std::runtime_error("event mismatch at cycle "+std::to_string(cycle));
            events+=want.size();++cycles;
        }
        std::cout<<"{\"events\":"<<events<<",\"cycles\":"<<cycles
                 <<",\"comparison\":\"exact chronological multiset\"}\n";
        return 0;
    } catch(const std::exception& error) {
        std::cerr<<error.what()<<'\n';return 1;
    }
}
