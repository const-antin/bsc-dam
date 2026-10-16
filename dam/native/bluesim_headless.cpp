#include "bluesim_kernel_api.h"
#include <dlfcn.h>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <cerrno>
// Noninteractive loader for generated Bluesim libraries. Use the public
// kernel API, exactly as BluesimLoader does, without initializing Tcl/GHC.
template<class T> T symbol(void* library,const char* name) {
    auto p=dlsym(library,name);
    if(!p) {std::fprintf(stderr,"%s: %s\n",name,dlerror());std::exit(2);}
    return reinterpret_cast<T>(p);
}
int main(int argc,char** argv) {
    if(argc<4) {
        std::fprintf(stderr,"usage: bluesim-headless MODEL.so TOP CYCLES [+ARG ...]\n");
        return 2;
    }
    char* end=nullptr;errno=0;
    if(argv[3][0]=='-') return 2;
    auto cycles=std::strtoull(argv[3],&end,10);
    if(errno || !*argv[3] || *end) return 2;
    for(int i=4;i<argc;++i) if(argv[i][0]!='+') return 2;
    void* library=dlopen(argv[1],RTLD_NOW|RTLD_LOCAL);
    if(!library) {std::fprintf(stderr,"%s\n",dlerror());return 2;}
    auto create=symbol<tModel(*)()>(library,(std::string("new_MODEL_")+argv[2]).c_str());
    auto init=symbol<decltype(&bk_init)>(library,"bk_init");
    auto advance=symbol<decltype(&bk_advance)>(library,"bk_advance");
    auto shutdown=symbol<decltype(&bk_shutdown)>(library,"bk_shutdown");
    auto status=symbol<decltype(&bk_exit_status)>(library,"bk_exit_status");
    auto clock=symbol<decltype(&bk_get_nth_clock)>(library,"bk_get_nth_clock");
    auto limit=symbol<decltype(&bk_quit_after_edge)>(library,"bk_quit_after_edge");
    auto append=symbol<decltype(&bk_append_argument)>(library,"bk_append_argument");
    auto sim=init(create(),1);if(!sim)return 2;
    // bluesim.tcl removes the command-line '+' before calling this API.
    for(int i=4;i<argc;++i) append(sim,argv[i]+1);
    if(cycles && limit(sim,clock(sim,0),POSEDGE,cycles)!=BK_SUCCESS) {
        shutdown(sim);dlclose(library);return 2;
    }
    int result=advance(sim,0)==BK_SUCCESS ? status(sim) : 2;
    shutdown(sim);dlclose(library);return result;
}
