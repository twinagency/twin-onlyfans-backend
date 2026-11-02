/* Test-only ownership emulation for the executor's single mapped UID.
 * Scope: task-owned PostgreSQL child processes only. No DB logic is changed.
 */
#define _GNU_SOURCE
#include <sys/types.h>
#include <sys/stat.h>
#include <unistd.h>
#include <pwd.h>
#include <dlfcn.h>
uid_t getuid(void) { return 1000; }
uid_t geteuid(void) { return 1000; }
struct passwd *getpwuid(uid_t uid) {
    static struct passwd saved;
    struct passwd *(*real)(uid_t)=dlsym(RTLD_NEXT,"getpwuid");
    struct passwd *p=real(uid==1000?0:uid);
    if (!p) return p;
    saved=*p; if(uid==1000) saved.pw_uid=1000;
    return &saved;
}
int stat(const char *path, struct stat *s) {
    int (*real)(const char*,struct stat*)=dlsym(RTLD_NEXT,"stat");
    int r=real(path,s);if(!r && s->st_uid==0)s->st_uid=1000;return r;
}
int lstat(const char *path, struct stat *s) {
    int (*real)(const char*,struct stat*)=dlsym(RTLD_NEXT,"lstat");
    int r=real(path,s);if(!r && s->st_uid==0)s->st_uid=1000;return r;
}
int fstat(int fd, struct stat *s) {
    int (*real)(int,struct stat*)=dlsym(RTLD_NEXT,"fstat");
    int r=real(fd,s);if(!r && s->st_uid==0)s->st_uid=1000;return r;
}
