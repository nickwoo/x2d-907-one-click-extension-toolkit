/* Fixed loopback-only transport for the lens profile worker.
 * Mirrors src/speed-buff-server.c: action-only request lines are mapped
 * to fixed worker argv, no request body is parsed, and no request text
 * becomes a shell command. The one deliberate difference is argument
 * validation: /select/ carries a profile id and an aperture in the path
 * (the controller's exact API), so those two segments are extracted and
 * checked against a fixed charset / fixed aperture list before they are
 * interpolated into the worker command. Any other character fails closed
 * to the fallback body, exactly like an unknown action in the speed
 * server.
 *
 * The worker prints its JSON (status/select) or raw log lines (log) to
 * stdout, so the server redirects that stdout to a temp file and returns
 * the file, mirroring how the speed server returns /tmp/x2d-speed-buff/
 * ui.json after running its worker. The log body is returned as
 * text/plain; status/select as application/json.
 *
 * Compile-time worker path override (host tests only): build with
 * -DX2D_LENS_WORKER='"/path/to/worker"' to point at a sandboxed worker.
 * The shipped default is the camera path below.
 */
typedef unsigned long size_t;
typedef long ssize_t;
extern int socket(int,int,int),bind(int,const void*,unsigned),listen(int,int),accept(int,void*,void*),close(int),setsockopt(int,int,int,const void*,unsigned);
extern ssize_t read(int,void*,size_t),write(int,const void*,size_t);
extern int open(const char*,int,...),strcmp(const char*,const char*),strncmp(const char*,const char*,size_t),system(const char*);
extern char *getenv(const char*);
extern int unsetenv(const char*),unlink(const char*);
extern void _exit(int);
extern int snprintf(char*,size_t,const char*,...);
struct addr {unsigned short family,port;unsigned int ip;char pad[8];};
struct timeval {long sec,usec;};
#ifndef X2D_LENS_WORKER
#define X2D_LENS_WORKER "/system/etc/x2d-lens-profile/worker"
#endif
#ifndef X2D_LENS_SH
#define X2D_LENS_SH "/system/bin/sh"
#endif
static const char *fallback="{\"ready\":false,\"message\":\"镜头配置文件服务拒绝操作，请恢复原状\"}";
static const char *out_json="/tmp/x2d-lens-profile/ui.json";
static const char *out_log="/tmp/x2d-lens-profile/log.txt";
static int valid_id(const char *id,int len) {
 int i;
 if(len<1||len>64)return 0;
 for(i=0;i<len;i++) {
  char c=id[i];
  if(!((c>='a'&&c<='z')||(c>='0'&&c<='9')||c=='-'))return 0;
 }
 return 1;
}
static int valid_aperture(const char *ap,int len) {
 /* Full-stop set from the controller; compare as text, never as float. */
 char buf[8];int i;
 if(len<1||len>4)return 0;
 for(i=0;i<len;i++)buf[i]=ap[i];
 buf[len]=0;
 return !strcmp(buf,"1.4")||!strcmp(buf,"2")||!strcmp(buf,"2.8")||!strcmp(buf,"4")||
        !strcmp(buf,"5.6")||!strcmp(buf,"8")||!strcmp(buf,"11")||!strcmp(buf,"16")||
        !strcmp(buf,"22")||!strcmp(buf,"32");
}
static int run(const char *action,const char *id,int idlen,const char *ap,int aplen) {
 char cmd[512];char idbuf[65];char apbuf[8];int i;
 if(!strcmp(action,"status")||!strcmp(action,"log")) {
  const char *out=!strcmp(action,"log")?out_log:out_json;
  snprintf(cmd,sizeof(cmd),"mkdir -p /tmp/x2d-lens-profile && %s %s %s > %s 2>/dev/null",
           X2D_LENS_SH,X2D_LENS_WORKER,action,out);
  return system(cmd)==0?0:-1;
 }
 if(strcmp(action,"select"))return -1;
 if(!valid_id(id,idlen))return -1;
 for(i=0;i<idlen;i++)idbuf[i]=id[i];
 idbuf[idlen]=0;
 if(aplen>0) {
  if(!valid_aperture(ap,aplen))return -1;
  for(i=0;i<aplen;i++)apbuf[i]=ap[i];
  apbuf[aplen]=0;
  snprintf(cmd,sizeof(cmd),"mkdir -p /tmp/x2d-lens-profile && %s %s select %s %s > %s 2>/dev/null",
           X2D_LENS_SH,X2D_LENS_WORKER,idbuf,apbuf,out_json);
 } else {
  snprintf(cmd,sizeof(cmd),"mkdir -p /tmp/x2d-lens-profile && %s %s select %s > %s 2>/dev/null",
           X2D_LENS_SH,X2D_LENS_WORKER,idbuf,out_json);
 }
 return system(cmd)==0?0:-1;
}
__attribute__((constructor)) static void serve(void) {
 const char *env=getenv("X2D_LENS_SERVER");if(!env||strcmp(env,"1"))return;
 int s=socket(2,1,0),one=1;if(s<0)_exit(70);
 setsockopt(s,1,2,&one,sizeof(one));
 struct addr a={2,0x4f49,0x0100007f,{0}}; /* 18767, 127.0.0.1 */
 if(bind(s,&a,sizeof(a))||listen(s,4)){close(s);_exit(71);}
 unsetenv("LD_PRELOAD");unsetenv("X2D_LENS_SERVER");
 /* No startup reapply: unlike the speed function, lens selection is not
  * an enabled flag to reapply. It persists in the /blackbox marker and
  * is read back on every status request, so a restart needs no action. */
 for(;;){
  int c=accept(s,0,0);if(c<0)continue;
  struct timeval t={3,0};setsockopt(c,1,20,&t,sizeof(t));setsockopt(c,1,21,&t,sizeof(t));
  char req[512]={0};ssize_t n=read(c,req,sizeof(req)-1);
  char action[16]={0};char id[65]={0};char ap[8]={0};int idlen=0;int aplen=0;int ok=0;int is_log=0;
  if(n>0){
   if(!strncmp(req,"GET /status HTTP/1.",19)){snprintf(action,sizeof(action),"%s","status");}
   else if(!strncmp(req,"GET /log HTTP/1.",16)){snprintf(action,sizeof(action),"%s","log");}
   else if(!strncmp(req,"POST /select/",13)) {
    const char *p=req+13;const char *slash=p;const char *end=p;
    while(*end&&*end!=' '&&*end!='\r'&&*end!='\n')end++;
    while(slash<end&&*slash!='/')slash++;
    idlen=(int)(slash-p);
    if(slash<end){aplen=(int)(end-(slash+1));}
    if(idlen>=1&&idlen<=64) {
     int i;for(i=0;i<idlen;i++)id[i]=p[i];
     if(aplen>0&&aplen<=4){for(i=0;i<aplen;i++)ap[i]=slash[1+i];}
     snprintf(action,sizeof(action),"%s","select");
    }
   }
  }
  if(action[0]) {
   ok=run(action,id,idlen,ap,aplen)==0;
   is_log=!strcmp(action,"log");
  }
  char body[4096];int len=0;
  if(ok) {
   int f=open(is_log?out_log:out_json,0);
   if(f>=0){len=(int)read(f,body,sizeof(body)-1);close(f);}
   /* An empty log is a valid empty log, not a failure. An empty
    * status/select body is a failure and falls through to fallback. */
   if(len<=0&&!is_log)ok=0;
  }
  if(!ok){len=snprintf(body,sizeof(body),"%s",fallback);is_log=0;}
  char header[256];int h=snprintf(header,sizeof(header),
    "HTTP/1.1 200 OK\r\nContent-Type: %s; charset=utf-8\r\nAccess-Control-Allow-Origin: *\r\nCache-Control: no-store\r\nContent-Length: %d\r\nConnection: close\r\n\r\n",
    is_log?"text/plain":"application/json",len<0?0:len);
  write(c,header,h);if(len>0)write(c,body,len);close(c);
 }
}
