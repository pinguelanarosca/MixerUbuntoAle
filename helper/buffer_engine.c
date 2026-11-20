/* Mixer Rápido: synchronous PipeWire DSP delay. SPDX-License-Identifier: MIT */
#define _GNU_SOURCE
#include <pipewire/pipewire.h>
#include <pipewire/filter.h>
#include <pipewire/extensions/metadata.h>
#include <spa/param/latency-utils.h>
#include <spa/param/audio/format-utils.h>
#include <spa/param/port-config.h>
#include <spa/param/props.h>
#include <stdatomic.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>
#include <signal.h>
#include <math.h>
#include <time.h>
#include <errno.h>

#define MAX_PORTS 2048
#define CAPACITY (48000u * 32u)
#define MAGIC 0x4d584234u

struct header {
    uint32_t magic, version, rate, channels, capacity, delay_frames;
    _Atomic uint64_t write, read, input_frames, output_frames;
    _Atomic uint64_t first_signal, first_input_ns, first_output_ns, decision_ns;
    _Atomic uint64_t overruns, last_process_ns;
    _Atomic uint32_t decision, gain_bits, muted, released;
    float first_input[2], first_output[2];
    uint32_t cookie;
    _Atomic uint32_t output_ready;
    _Atomic uint64_t command;
    uint64_t applied_command;
    uint64_t first_decision_ns;
    uint32_t first_decision, first_gain_bits;
    uint32_t quantum;
    uint32_t source_initialised;
    uint32_t release_started, ramp_left;
    float current_gain, target_gain;
};
struct manager;
struct flow {
    struct manager *manager;
    struct flow *next;
    char serial[32], name[128], target[256], directory[512];
    struct pw_filter *filter;
    struct spa_hook listener;
    void *input[2], *output[2];
    struct pw_proxy *links[2];
    struct pw_proxy *input_links[2];
    struct pw_node *source_proxy;
    struct spa_hook source_listener;
    bool source_configured;
    bool recovering;
    bool backpressure;
    _Atomic bool source_corked;
    uint32_t target_id, source_id, node_id, delay_ms, max_hold_ms, latency_frames;
    uint64_t removed_ns, removed_frame, control_mtime;
    bool direct;
    int activate_seq;
    bool activation_requested;
    size_t map_size;
    struct header *h;
    float *samples;
};
struct port { uint32_t id, node; bool input; char channel[32]; };
struct node {uint32_t id;uint64_t serial;char name[256];};
struct manager {
    struct pw_main_loop *loop;
    struct pw_context *context;
    struct pw_core *core;
    struct pw_registry *registry;
    struct spa_hook registry_listener, core_listener, metadata_listener;
    struct pw_metadata *metadata;
    uint32_t metadata_id;
    struct flow *flows;
    struct port ports[MAX_PORTS];
    unsigned nports;
    struct node nodes[MAX_PORTS];unsigned nnodes;uint32_t cookie;
    char directory[512];
    uint32_t delay_ms, max_hold_ms;
    float safe_percent;
    bool direct_only;
    struct spa_source *timer;
};

static uint64_t now_ns(void) {
    struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000u + ts.tv_nsec;
}
static uint32_t bits(float f) { uint32_t b; memcpy(&b, &f, 4); return b; }
static float unbits(uint32_t b) { float f; memcpy(&f, &b, 4); return f; }
static void set_gain(struct flow *f, float percent, uint32_t decision, bool mute) {
    if (!isfinite(percent) || percent < 0 || percent > 150) return;
    float v = percent / 100.0f;
    uint64_t command=bits(v*v*v)|((uint64_t)decision<<32)|((uint64_t)mute<<40);
    atomic_store_explicit(&f->h->command,command,memory_order_release);
}

static void process(void *userdata, struct spa_io_position *position) {
    struct flow *f = userdata;
    struct header *h = f->h;
    uint32_t n = position->clock.duration;
    uint32_t rate = position->clock.rate.denom;
    if (rate && !h->rate) {
        h->rate = rate; h->delay_frames = (uint64_t)rate * f->delay_ms / 1000;
    }
    float *in[2], *out[2];
    for (int c=0;c<2;c++) {
        in[c] = pw_filter_get_dsp_buffer(f->input[c], n);
        out[c] = pw_filter_get_dsp_buffer(f->output[c], n);
    }
    uint64_t w = atomic_load_explicit(&h->write, memory_order_relaxed);
    uint64_t r = atomic_load_explicit(&h->read, memory_order_relaxed);
    uint64_t stamp = now_ns();
    bool output = out[0] && out[1] && atomic_load_explicit(&h->output_ready,memory_order_acquire);
    bool input = in[0] || in[1];
    if(f->recovering&&!input&&!atomic_load(&f->source_corked)&&!f->removed_ns) {
        for(int c=0;c<2;c++)if(out[c])memset(out[c],0,n*sizeof(float));
        atomic_store(&h->last_process_ns,stamp);h->quantum=n;
        return; /* reconnect is not input PCM: never insert it into the queue */
    }
    if(input)f->recovering=false;
    for (uint32_t i=0; i<n; i++) {
        uint64_t command=atomic_load_explicit(&h->command,memory_order_acquire);
        if(command!=h->applied_command) {
            h->applied_command=command;
            atomic_store(&h->gain_bits,(uint32_t)command);
            atomic_store(&h->decision,(command>>32)&255);
            atomic_store(&h->muted,(command>>40)&1);
            atomic_store(&h->decision_ns,stamp+(uint64_t)i*1000000000u/rate);
            h->target_gain=unbits((uint32_t)command);
            if(h->release_started&&((command>>32)&255)>=2&&((command>>32)&255)<=3) {
                h->ramp_left=rate*120/1000; /* automatic changes: 120ms, no step */
            } else {h->current_gain=h->target_gain;h->ramp_left=0;}
        }
        if(h->ramp_left) {
            h->current_gain+=(h->target_gain-h->current_gain)/h->ramp_left;
            h->ramp_left--;
        }
        float gain=h->current_gain;
        if((command>>40)&1)gain=0;
        float x[2] = { in[0] ? in[0][i] : 0, in[1] ? in[1][i] : 0 };
        for (int c=0;c<2;c++) if (!isfinite(x[c])) x[c] = 0;
        if (w-r >= CAPACITY) {
            atomic_fetch_add(&h->overruns, 1);
            if (output) out[0][i] = out[1][i] = 0;
            continue;
        }
        if (input && !atomic_load(&h->first_input_ns) && (x[0] != 0 || x[1] != 0)) {
            h->first_input[0] = x[0]; h->first_input[1] = x[1];
            atomic_store(&h->first_signal, w);
            atomic_store(&h->first_input_ns, stamp + (uint64_t)i*1000000000u/rate);
        }
        f->samples[(w % CAPACITY)*2] = x[0];
        f->samples[(w % CAPACITY)*2+1] = x[1];
        w++;
        if (output) {
            if (w-r > h->delay_frames) {
                if(!h->release_started&&atomic_load(&h->first_input_ns)&&r>=atomic_load(&h->first_signal)) {
                    uint64_t age=w-atomic_load(&h->first_signal);
                    uint64_t limit=(uint64_t)rate*f->max_hold_ms/1000;
                    if(((command>>32)&255)<2&&age<limit) {
                        out[0][i]=out[1][i]=0;
                        continue; /* retain PCM, including the complete first word */
                    }
                    /* Freeze this initial delay for the rest of this flow.
                     * Never catch up by dropping frames or changing the rate. */
                    h->delay_frames=w-r-1;h->release_started=1;
                    if(atomic_load(&h->decision)==0) {
                        atomic_store(&h->decision,1);
                        atomic_store(&h->decision_ns,stamp+(uint64_t)i*1000000000u/rate);
                    }
                }
                float y0 = f->samples[(r % CAPACITY)*2] * gain;
                float y1 = f->samples[(r % CAPACITY)*2+1] * gain;
                out[0][i] = y0; out[1][i] = y1;
                if (!atomic_load(&h->first_output_ns) && (y0 != 0 || y1 != 0)) {
                    if (atomic_load_explicit(&h->decision, memory_order_acquire) == 0) {
                        atomic_store(&h->decision_ns, stamp);
                        atomic_store(&h->decision, 1); /* native safe timeout */
                    }
                    h->first_output[0]=y0; h->first_output[1]=y1;
                    h->first_decision_ns=atomic_load(&h->decision_ns);
                    h->first_decision=atomic_load(&h->decision);
                    h->first_gain_bits=bits(gain);
                    atomic_store(&h->first_output_ns, stamp+(uint64_t)i*1000000000u/rate);
                    atomic_store(&h->released, 1);
                }
                r++;
                atomic_fetch_add_explicit(&h->output_frames, 1, memory_order_relaxed);
            } else out[0][i] = out[1][i] = 0;
        }
    }
    if (input) atomic_fetch_add_explicit(&h->input_frames, n, memory_order_relaxed);
    atomic_store_explicit(&h->write, w, memory_order_release);
    atomic_store_explicit(&h->read, r, memory_order_release);
    atomic_store(&h->last_process_ns, stamp);
    h->quantum=n;
}

static void state_changed(void *userdata, enum pw_filter_state old, enum pw_filter_state state, const char *error) {
    struct flow *f = userdata;
    f->node_id = pw_filter_get_node_id(f->filter);
    if (state == PW_FILTER_STATE_ERROR) fprintf(stderr,"buffer %s: %s\n",f->serial,error);
}
static const struct pw_filter_events filter_events = {
    PW_VERSION_FILTER_EVENTS, .state_changed=state_changed, .process=process,
};

static void status(struct flow *f) {
    char path[1024], tmp[1024];
    snprintf(path,sizeof(path),"%s/status-%s.json",f->directory,f->serial);
    snprintf(tmp,sizeof(tmp),"%s/.status-%s.tmp",f->directory,f->serial);
    FILE *o = fopen(tmp,"w"); if (!o) return;
    struct header *h=f->h;
    float gain=unbits(atomic_load(&h->gain_bits));
    fprintf(o,"{\"serial\":\"%s\",\"pid\":%d,\"cookie\":%u,\"node_id\":%u,\"delay_ms\":%u,\"rate\":%u,"
              "\"write\":%lu,\"read\":%lu,\"first_signal_frame\":%lu,\"input_frames\":%lu,\"output_frames\":%lu,"
              "\"first_input_ns\":%lu,\"first_output_ns\":%lu,\"decision_ns\":%lu,\"decision\":%u,"
              "\"volume\":%.6f,\"mute\":%s,\"first_input\":[%.9g,%.9g],\"first_output\":[%.9g,%.9g],\"overruns\":%lu,"
              "\"first_decision_ns\":%lu,\"first_decision\":%u,\"first_volume\":%.6f,\"last_process_ns\":%lu,\"quantum\":%u,\"actual_delay_ms\":%.3f,\"max_hold_ms\":%u,\"ramp_left\":%u}\n",
        f->serial,getpid(),f->h->cookie,f->node_id,f->delay_ms,h->rate,atomic_load(&h->write),atomic_load(&h->read),atomic_load(&h->first_signal),
        atomic_load(&h->input_frames),atomic_load(&h->output_frames),atomic_load(&h->first_input_ns),atomic_load(&h->first_output_ns),
        atomic_load(&h->decision_ns),atomic_load(&h->decision),cbrtf(gain)*100,atomic_load(&h->muted)?"true":"false",
        h->first_input[0],h->first_input[1],h->first_output[0],h->first_output[1],atomic_load(&h->overruns),
        h->first_decision_ns,h->first_decision,cbrtf(unbits(h->first_gain_bits))*100,atomic_load(&h->last_process_ns),h->quantum,
        h->rate?(double)h->delay_frames*1000/h->rate:0,f->max_hold_ms,h->ramp_left);
    fclose(o); rename(tmp,path);
}

static struct flow *create_flow(struct manager *m, const char *serial, const char *target, uint32_t target_id, bool direct) {
    for (struct flow *f=m->flows;f;f=f->next) if (!strcmp(f->serial,serial)) return f;
    struct flow *f=calloc(1,sizeof(*f)); if (!f) return NULL;
    f->manager=m; f->target_id=target_id; f->node_id=PW_ID_ANY; f->delay_ms=m->delay_ms; f->direct=direct;
    f->max_hold_ms=SPA_MAX(m->max_hold_ms,m->delay_ms);
    snprintf(f->serial,sizeof(f->serial),"%s",serial);
    snprintf(f->name,sizeof(f->name),"mixer.buffer.%s",serial);
    snprintf(f->target,sizeof(f->target),"%s",target ? target : "");
    snprintf(f->directory,sizeof(f->directory),"%s",m->directory);
    char filename[1024]; snprintf(filename,sizeof(filename),"%s/audio-%s.shm",m->directory,serial);
    int fd=open(filename,O_RDWR|O_CREAT,0600); if(fd<0) { free(f);return NULL; }
    f->map_size=4096+(size_t)CAPACITY*2*sizeof(float);
    if(ftruncate(fd,f->map_size)<0) {close(fd);free(f);return NULL;}
    f->h=mmap(NULL,f->map_size,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0);close(fd);
    if(f->h==MAP_FAILED){free(f);return NULL;}
    f->samples=(float *)((char *)f->h+4096);
    if(f->h->magic==MAGIC&&f->h->cookie==m->cookie&&f->h->version==2&&atomic_load(&f->h->first_input_ns))f->recovering=true;
    if(f->h->magic!=MAGIC||f->h->cookie!=m->cookie||f->h->version!=2) {
        memset(f->h,0,f->map_size);
        f->h->version=2; f->h->channels=2;f->h->capacity=CAPACITY;
        f->h->magic=MAGIC;f->h->cookie=m->cookie;atomic_store(&f->h->gain_bits,bits(powf(m->safe_percent/100,3)));
        atomic_store(&f->h->command,bits(powf(m->safe_percent/100,3)));
    }
    if(f->h->applied_command&&f->h->current_gain==0&&f->h->target_gain==0) {
        /* Appended header fields: preserve gain and the established delay when
         * upgrading an existing queue written by the previous engine. */
        f->h->current_gain=f->h->target_gain=unbits((uint32_t)atomic_load(&f->h->command));
        f->h->release_started=atomic_load(&f->h->released);
    }
    atomic_store(&f->h->output_ready,0);
    f->filter=pw_filter_new(m->core,"Mixer buffer",pw_properties_new(
        PW_KEY_NODE_NAME,f->name,PW_KEY_NODE_DESCRIPTION,f->name,
        PW_KEY_MEDIA_TYPE,"Audio",PW_KEY_MEDIA_CATEGORY,"Filter",PW_KEY_MEDIA_CLASS,"Audio/Sink",
        PW_KEY_NODE_VIRTUAL,"true",PW_KEY_NODE_ALWAYS_PROCESS,"true",
        "wireplumber.is-virtual","true",
        "node.hidden","true","priority.session","-10000","mixer.buffer.serial",serial,
        "node.pause-on-idle","false","node.suspend-on-idle","false",NULL));
    if(!f->filter) {munmap(f->h,f->map_size);free(f);return NULL;}
    pw_filter_add_listener(f->filter,&f->listener,&filter_events,f);
    for(int c=0;c<2;c++) {
        const char *ch=c==0?"FL":"FR";
        f->input[c]=pw_filter_add_port(f->filter,PW_DIRECTION_INPUT,PW_FILTER_PORT_FLAG_MAP_BUFFERS,1,
            pw_properties_new(PW_KEY_FORMAT_DSP,"32 bit float mono audio",PW_KEY_PORT_NAME,c==0?"input_FL":"input_FR","audio.channel",ch,NULL),NULL,0);
        f->output[c]=pw_filter_add_port(f->filter,PW_DIRECTION_OUTPUT,PW_FILTER_PORT_FLAG_MAP_BUFFERS,1,
            pw_properties_new(PW_KEY_FORMAT_DSP,"32 bit float mono audio",PW_KEY_PORT_NAME,c==0?"output_FL":"output_FR","audio.channel",ch,NULL),NULL,0);
    }
    uint8_t buffer[1024];struct spa_pod_builder b=SPA_POD_BUILDER_INIT(buffer,sizeof(buffer));
    const struct spa_pod *params[2];
    struct spa_audio_info_raw info={.format=SPA_AUDIO_FORMAT_F32P,.rate=48000,.channels=2,.position={SPA_AUDIO_CHANNEL_FL,SPA_AUDIO_CHANNEL_FR}};
    params[0]=spa_format_audio_raw_build(&b,SPA_PARAM_EnumFormat,&info);
    params[1]=spa_process_latency_build(&b,SPA_PARAM_ProcessLatency,&SPA_PROCESS_LATENCY_INFO_INIT(.ns=(uint64_t)m->delay_ms*1000000));
    f->next=m->flows;m->flows=f;
    if(pw_filter_connect(f->filter,PW_FILTER_FLAG_RT_PROCESS|PW_FILTER_FLAG_INACTIVE,params,2)<0) fprintf(stderr,"filter connect failed\n");
    fprintf(stderr,"created buffer %s target %s (%u)\n",serial,f->target,target_id);
    return f;
}

static void source_info(void *userdata,const struct pw_node_info *info) {
    struct flow *f=userdata;const char *c=info->props?spa_dict_lookup(info->props,"pulse.corked"):NULL;
    if(c)atomic_store(&f->source_corked,!strcmp(c,"true"));
}
static const struct pw_node_events source_events={PW_VERSION_NODE_EVENTS,.info=source_info};

static void link_output(struct flow *f) {
    struct manager *m=f->manager;
    if(f->node_id==PW_ID_ANY || f->target_id==PW_ID_ANY) return;
    for(int c=0;c<2;c++) {
        if(f->links[c]) continue;
        const char *ch=c==0?"FL":"FR";struct port *out=NULL,*in=NULL;
        for(unsigned i=0;i<m->nports;i++) {
            struct port *p=&m->ports[i];
            if(p->node==f->node_id&&!p->input&&!strcmp(p->channel,ch))out=p;
            if(p->node==f->target_id&&p->input&&!strcmp(p->channel,ch))in=p;
        }
        if(!out||!in)continue;
        struct pw_properties *p=pw_properties_new(NULL,NULL);
        pw_properties_setf(p,PW_KEY_LINK_OUTPUT_NODE,"%u",f->node_id);
        pw_properties_setf(p,PW_KEY_LINK_OUTPUT_PORT,"%u",out->id);
        pw_properties_setf(p,PW_KEY_LINK_INPUT_NODE,"%u",f->target_id);
        pw_properties_setf(p,PW_KEY_LINK_INPUT_PORT,"%u",in->id);
        f->links[c]=pw_core_create_object(m->core,"link-factory",PW_TYPE_INTERFACE_Link,PW_VERSION_LINK,&p->dict,0);
        pw_properties_free(p);
    }
    if(f->source_id!=PW_ID_ANY&&!f->source_configured) {
        unsigned count=0;for(unsigned i=0;i<m->nports;i++)if(m->ports[i].node==f->source_id&&!m->ports[i].input)count++;
        if(!f->source_proxy) {
            f->source_proxy=pw_registry_bind(m->registry,f->source_id,PW_TYPE_INTERFACE_Node,PW_VERSION_NODE,0);
            pw_node_add_listener(f->source_proxy,&f->source_listener,&source_events,f);
        }
        if(!f->h->source_initialised) {
            uint8_t buf[256];struct spa_pod_builder b=SPA_POD_BUILDER_INIT(buf,sizeof(buf));float vols[2]={1,1};
            const struct spa_pod *p=spa_pod_builder_add_object(&b,SPA_TYPE_OBJECT_Props,SPA_PARAM_Props,
                SPA_PROP_channelVolumes,SPA_POD_Array(sizeof(float),SPA_TYPE_Float,2,vols));
            pw_node_set_param(f->source_proxy,SPA_PARAM_Props,0,p);
        }
        if(count<2) {
            uint8_t fb[512],pb[1024];struct spa_pod_builder fmtb=SPA_POD_BUILDER_INIT(fb,sizeof(fb)),b=SPA_POD_BUILDER_INIT(pb,sizeof(pb));
            struct spa_audio_info_raw info={.format=SPA_AUDIO_FORMAT_F32P,.rate=48000,.channels=2,.position={SPA_AUDIO_CHANNEL_FL,SPA_AUDIO_CHANNEL_FR}};
            const struct spa_pod *fmt=spa_format_audio_raw_build(&fmtb,SPA_PARAM_Format,&info);
            const struct spa_pod *param=spa_pod_builder_add_object(&b,SPA_TYPE_OBJECT_ParamPortConfig,SPA_PARAM_PortConfig,
                SPA_PARAM_PORT_CONFIG_direction,SPA_POD_Id(SPA_DIRECTION_OUTPUT),
                SPA_PARAM_PORT_CONFIG_mode,SPA_POD_Id(SPA_PARAM_PORT_CONFIG_MODE_dsp),
                SPA_PARAM_PORT_CONFIG_monitor,SPA_POD_Bool(false),
                SPA_PARAM_PORT_CONFIG_control,SPA_POD_Bool(false),
                SPA_PARAM_PORT_CONFIG_format,SPA_POD_Pod(fmt));
            pw_node_set_param(f->source_proxy,SPA_PARAM_PortConfig,0,param);
        }
        f->source_configured=true;
    }
    if(f->source_id!=PW_ID_ANY)for(int c=0;c<2;c++) {
        if(f->input_links[c])continue;
        const char *ch=c==0?"FL":"FR";struct port *out=NULL,*in=NULL;
        for(unsigned i=0;i<m->nports;i++) {
            struct port *p=&m->ports[i];
            if(p->node==f->source_id&&!p->input&&!strcmp(p->channel,ch))out=p;
            if(p->node==f->node_id&&p->input&&!strcmp(p->channel,ch))in=p;
        }
        if(!out||!in)continue;
        struct pw_properties *p=pw_properties_new(NULL,NULL);
        pw_properties_setf(p,PW_KEY_LINK_OUTPUT_NODE,"%u",f->source_id);
        pw_properties_setf(p,PW_KEY_LINK_OUTPUT_PORT,"%u",out->id);
        pw_properties_setf(p,PW_KEY_LINK_INPUT_NODE,"%u",f->node_id);
        pw_properties_setf(p,PW_KEY_LINK_INPUT_PORT,"%u",in->id);
        f->input_links[c]=pw_core_create_object(m->core,"link-factory",PW_TYPE_INTERFACE_Link,PW_VERSION_LINK,&p->dict,0);
        pw_properties_free(p);
    }
    if(f->links[0]&&f->links[1]&&f->input_links[0]&&f->input_links[1]&&!f->activation_requested) {
        f->activation_requested=true;
        f->activate_seq=pw_core_sync(m->core,PW_ID_CORE,0);
    }
}

static int metadata_changed(void *userdata, uint32_t subject, const char *key, const char *type, const char *value) {
    struct manager *m=userdata;
    if(!key)return 0;
    if(!strcmp(key,"mixer.buffer.policy")&&value) {
        struct pw_properties *p=pw_properties_new_string(value);
        if(p) {const char *d=pw_properties_get(p,"delay_ms"),*v=pw_properties_get(p,"safe_volume"),*h=pw_properties_get(p,"max_hold_ms");
            if(d){unsigned x=strtoul(d,NULL,10);if(x>=100&&x<=5000)m->delay_ms=x;}
            if(v){float x=strtof(v,NULL);if(isfinite(x)&&x>=0&&x<=100)m->safe_percent=x;}
            if(h){unsigned x=strtoul(h,NULL,10);if(x>=500&&x<=5000)m->max_hold_ms=x;}
            pw_properties_free(p);}
        return 0;
    }
    if(strncmp(key,"mixer.buffer.request.",21))return 0;
    if(!value) {for(struct flow *f=m->flows;f;f=f->next)if(!strcmp(f->serial,key+21)){f->removed_ns=now_ns();f->removed_frame=atomic_load(&f->h->write);}return 0;}
    struct pw_properties *p=pw_properties_new_string(value);if(!p)return 0;
    const char *serial=pw_properties_get(p,"serial"),*target=pw_properties_get(p,"target"),*id=pw_properties_get(p,"target_id");
    if(serial&&id&&strspn(serial,"0123456789")==strlen(serial)) {
        const char *source=pw_properties_get(p,"source_id");bool valid=false;
        if(source)for(unsigned i=0;i<m->nnodes;i++)if(m->nodes[i].id==strtoul(source,NULL,10)&&m->nodes[i].serial==strtoull(serial,NULL,10))valid=true;
        if(!valid){pw_properties_free(p);return 0;}
        struct flow *f=create_flow(m,serial,target,(uint32_t)strtoul(id,NULL,10),false);
        if(f&&source)f->source_id=strtoul(source,NULL,10);
        if(f&&f->target_id!=(uint32_t)strtoul(id,NULL,10)) {
            pw_filter_set_active(f->filter,false);
            atomic_store(&f->h->output_ready,0);
            for(int c=0;c<2;c++)if(f->links[c]){pw_proxy_destroy(f->links[c]);f->links[c]=NULL;}
            f->target_id=strtoul(id,NULL,10);snprintf(f->target,sizeof(f->target),"%s",target?target:"");
            f->activation_requested=false;
        }
    }
    pw_properties_free(p);return 0;
}
static const struct pw_metadata_events metadata_events={PW_VERSION_METADATA_EVENTS,.property=metadata_changed};

static void global(void *userdata,uint32_t id,uint32_t permissions,const char *type,uint32_t version,const struct spa_dict *props) {
    struct manager *m=userdata;if(!props)return;
    if(!strcmp(type,PW_TYPE_INTERFACE_Node)&&m->nnodes<MAX_PORTS) {
        const char *serial=spa_dict_lookup(props,PW_KEY_OBJECT_SERIAL),*name=spa_dict_lookup(props,PW_KEY_NODE_NAME);
        if(serial) {struct node *n=&m->nodes[m->nnodes++];n->id=id;n->serial=strtoull(serial,NULL,10);snprintf(n->name,sizeof(n->name),"%s",name?name:"");}
        if(name)for(struct flow *f=m->flows;f;f=f->next)if(f->target_id==PW_ID_ANY&&!strcmp(f->target,name)){f->target_id=id;f->activation_requested=false;}
    } else if(!strcmp(type,PW_TYPE_INTERFACE_Port)&&m->nports<MAX_PORTS) {
        const char *nid=spa_dict_lookup(props,PW_KEY_NODE_ID),*dir=spa_dict_lookup(props,PW_KEY_PORT_DIRECTION),*ch=spa_dict_lookup(props,"audio.channel");
        if(!ch) {
            const char *name=spa_dict_lookup(props,PW_KEY_PORT_NAME);
            if(name&&strlen(name)>=2) {
                const char *suffix=name+strlen(name)-2;
                if(!strcmp(suffix,"FL")||!strcmp(suffix,"FR"))ch=suffix;
            }
        }
        if(nid&&dir&&ch) {struct port *p=&m->ports[m->nports++];p->id=id;p->node=strtoul(nid,NULL,10);p->input=!strcmp(dir,"in");snprintf(p->channel,sizeof(p->channel),"%s",ch);}
    } else if(!strcmp(type,PW_TYPE_INTERFACE_Metadata)) {
        const char *name=spa_dict_lookup(props,PW_KEY_METADATA_NAME);
        if(name&&!strcmp(name,"default")&&!m->metadata&&!m->direct_only) {
            m->metadata=pw_registry_bind(m->registry,id,type,PW_VERSION_METADATA,0);
            m->metadata_id=id;
            pw_metadata_add_listener(m->metadata,&m->metadata_listener,&metadata_events,m);
        }
    }
}
static void global_remove(void *userdata,uint32_t id) {
    struct manager *m=userdata;
    if(m->metadata&&m->metadata_id==id){spa_hook_remove(&m->metadata_listener);pw_proxy_destroy((struct pw_proxy*)m->metadata);m->metadata=NULL;}
    for(unsigned i=0;i<m->nnodes;)if(m->nodes[i].id==id)m->nodes[i]=m->nodes[--m->nnodes];else i++;
    for(unsigned i=0;i<m->nports;) {
        if(m->ports[i].id==id)m->ports[i]=m->ports[--m->nports];else i++;
    }
    for(struct flow *f=m->flows;f;f=f->next) {
        if(f->source_id==id&&!f->direct){f->removed_ns=now_ns();f->removed_frame=atomic_load(&f->h->write);}
        if(f->target_id==id) {f->target_id=PW_ID_ANY;atomic_store(&f->h->output_ready,0);for(int c=0;c<2;c++)if(f->links[c]){pw_proxy_destroy(f->links[c]);f->links[c]=NULL;}}
    }
}
static const struct pw_registry_events registry_events={PW_VERSION_REGISTRY_EVENTS,.global=global,.global_remove=global_remove};

static void tick(void *userdata,uint64_t expirations) {
    struct manager *m=userdata;
    for(struct flow *f=m->flows;f;f=f->next) {
        link_output(f);
        if(f->h->release_started&&f->h->rate&&f->latency_frames!=f->h->delay_frames) {
            uint8_t buf[256];struct spa_pod_builder b=SPA_POD_BUILDER_INIT(buf,sizeof(buf));
            const struct spa_pod *p=spa_process_latency_build(&b,SPA_PARAM_ProcessLatency,
                &SPA_PROCESS_LATENCY_INFO_INIT(.ns=(uint64_t)f->h->delay_frames*1000000000/f->h->rate));
            pw_filter_update_params(f->filter,NULL,&p,1);
            f->latency_frames=f->h->delay_frames;
        }
        if(!atomic_load(&f->h->output_ready)&&!f->backpressure&&
           atomic_load(&f->h->write)-atomic_load(&f->h->read)>CAPACITY-48000) {
            pw_filter_set_active(f->filter,false);
            f->backpressure=true; /* stop pulling the producer before overflow */
        }
        if(atomic_load(&f->h->output_ready)&&f->backpressure) {
            f->backpressure=false;pw_filter_set_active(f->filter,true);
        }
        char path[1024];snprintf(path,sizeof(path),"%s/control-%s.json",m->directory,f->serial);
        struct stat st;
        if(stat(path,&st)==0) {
            uint64_t mt=(uint64_t)st.st_mtim.tv_sec*1000000000+st.st_mtim.tv_nsec;
            if(mt!=f->control_mtime) {
                char buf[4096];FILE *in=fopen(path,"r");if(in) {
                    size_t n=fread(buf,1,sizeof(buf)-1,in);buf[n]=0;fclose(in);
                    struct pw_properties *p=pw_properties_new_string(buf);
                    if(p) {const char *v=pw_properties_get(p,"volume"),*d=pw_properties_get(p,"decision"),*mu=pw_properties_get(p,"mute");
                        if(v)set_gain(f,strtof(v,NULL),d?strtoul(d,NULL,10):2,mu&&!strcmp(mu,"true"));
                        pw_properties_free(p);}
                }
                f->control_mtime=mt;
            }
        }
        status(f);
    }
    struct flow **p=&m->flows;
    while(*p) {
        struct flow *f=*p;
        if(f->removed_ns&&atomic_load(&f->h->read)>=f->removed_frame&&
           now_ns()-f->removed_ns>((uint64_t)f->max_hold_ms+2000)*1000000u) {
            *p=f->next;
            for(int c=0;c<2;c++) {if(f->links[c])pw_proxy_destroy(f->links[c]);if(f->input_links[c])pw_proxy_destroy(f->input_links[c]);}
            if(f->source_proxy){spa_hook_remove(&f->source_listener);pw_proxy_destroy((struct pw_proxy*)f->source_proxy);}
            pw_filter_destroy(f->filter);munmap(f->h,f->map_size);
            char path[1024];snprintf(path,sizeof(path),"%s/audio-%s.shm",m->directory,f->serial);unlink(path);
            snprintf(path,sizeof(path),"%s/control-%s.json",m->directory,f->serial);unlink(path);
            snprintf(path,sizeof(path),"%s/status-%s.json",m->directory,f->serial);unlink(path);
            free(f);
        } else p=&f->next;
    }
}
static void quit(void *userdata,int sig){pw_main_loop_quit(((struct manager*)userdata)->loop);}
static void core_error(void *userdata,uint32_t id,int seq,int res,const char *message) {
    fprintf(stderr,"PipeWire: %s (%d)\n",message,res);
    if(id==PW_ID_CORE&&res==-EPIPE)pw_main_loop_quit(((struct manager*)userdata)->loop);
}
static void core_done(void *userdata,uint32_t id,int seq) {
    struct manager *m=userdata;
    for(struct flow *f=m->flows;f;f=f->next)if(f->activation_requested&&f->activate_seq==seq) {
        f->activate_seq=-1;f->h->source_initialised=1;atomic_store_explicit(&f->h->output_ready,1,memory_order_release);pw_filter_set_active(f->filter,true);
    }
}
static void core_info(void *userdata,const struct pw_core_info *info){((struct manager*)userdata)->cookie=info->cookie;}
static const struct pw_core_events core_events={PW_VERSION_CORE_EVENTS,.error=core_error,.done=core_done,.info=core_info};

int main(int argc,char **argv) {
    struct manager m={.delay_ms=500,.max_hold_ms=2000,.safe_percent=20};
    const char *runtime=getenv("XDG_RUNTIME_DIR");if(!runtime)return 2;
    snprintf(m.directory,sizeof(m.directory),"%s/mixer-buffer",runtime);mkdir(m.directory,0700);
    const char *serial=NULL,*target=NULL;uint32_t target_id=PW_ID_ANY,source_id=PW_ID_ANY;
    for(int i=1;i+1<argc;i+=2) {
        if(!strcmp(argv[i],"--serial"))serial=argv[i+1];
        else if(!strcmp(argv[i],"--target-id"))target_id=strtoul(argv[i+1],NULL,10);
        else if(!strcmp(argv[i],"--source-id"))source_id=strtoul(argv[i+1],NULL,10);
        else if(!strcmp(argv[i],"--target"))target=argv[i+1];
        else if(!strcmp(argv[i],"--delay-ms"))m.delay_ms=strtoul(argv[i+1],NULL,10);
        else if(!strcmp(argv[i],"--safe-volume"))m.safe_percent=strtof(argv[i+1],NULL);
        else if(!strcmp(argv[i],"--directory"))snprintf(m.directory,sizeof(m.directory),"%s",argv[i+1]);
    }
    if(m.delay_ms<100||m.delay_ms>5000||m.safe_percent<0||m.safe_percent>100)return 2;
    if(!serial) {
        char path[1024],buf[4096];const char *config=getenv("XDG_CONFIG_HOME"),*home=getenv("HOME");
        if(config)snprintf(path,sizeof(path),"%s/mixer-rapido/buffer-defaults.json",config);
        else snprintf(path,sizeof(path),"%s/.config/mixer-rapido/buffer-defaults.json",home?home:"");
        FILE *file=fopen(path,"r");if(file){size_t n=fread(buf,1,sizeof(buf)-1,file);buf[n]=0;fclose(file);
            struct pw_properties *p=pw_properties_new_string(buf);if(p){const char *d=pw_properties_get(p,"delay_ms"),*v=pw_properties_get(p,"safe_volume"),*h=pw_properties_get(p,"max_hold_ms");
                if(d){unsigned x=strtoul(d,NULL,10);if(x>=100&&x<=5000)m.delay_ms=x;}
                if(v){float x=strtof(v,NULL);if(isfinite(x)&&x>=0&&x<=100)m.safe_percent=x;}
                if(h){unsigned x=strtoul(h,NULL,10);if(x>=500&&x<=5000)m.max_hold_ms=x;}
                pw_properties_free(p);}}
    }
    mkdir(m.directory,0700);pw_init(NULL,NULL);
    m.loop=pw_main_loop_new(NULL);m.context=pw_context_new(pw_main_loop_get_loop(m.loop),NULL,0);
    m.core=pw_context_connect(m.context,pw_properties_new(PW_KEY_APP_NAME,"Mixer Rápido · buffer",NULL),0);
    if(!m.core)return 3;
    pw_core_add_listener(m.core,&m.core_listener,&core_events,&m);
    m.direct_only=serial!=NULL;
    m.registry=pw_core_get_registry(m.core,PW_VERSION_REGISTRY,0);
    pw_registry_add_listener(m.registry,&m.registry_listener,&registry_events,&m);
    if(serial){struct flow *f=create_flow(&m,serial,target,target_id,true);if(f)f->source_id=source_id;}
    struct pw_loop *loop=pw_main_loop_get_loop(m.loop);
    pw_loop_add_signal(loop,SIGINT,quit,&m);pw_loop_add_signal(loop,SIGTERM,quit,&m);
    m.timer=pw_loop_add_timer(loop,tick,&m);
    struct timespec interval={.tv_sec=0,.tv_nsec=10000000};pw_loop_update_timer(loop,m.timer,&interval,&interval,false);
    pw_main_loop_run(m.loop);
    for(struct flow *f=m.flows,*next;f;f=next) {next=f->next;status(f);pw_filter_destroy(f->filter);munmap(f->h,f->map_size);free(f);}
    pw_core_disconnect(m.core);pw_context_destroy(m.context);pw_main_loop_destroy(m.loop);pw_deinit();return 0;
}
