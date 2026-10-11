// braw2pipe -- decode a Blackmagic RAW clip to raw 16-bit RGB frames on stdout.
//
//   braw2pipe --info [--scale N] clip.braw    one JSON object: size, frames, fps, ...
//   braw2pipe        [--scale N] clip.braw    frames as rgb48le, in order, to stdout
//
// N = 1 (full), 2 (half), 4 (quarter), 8 (eighth). Sizes reported by --info are
// the scaled sizes. The stylet_core ingest stage pipes this into ffmpeg.
//
// Build against the free Blackmagic RAW SDK (Linux): see build.sh.
// STATUS: written from the SDK's documented C++ API and its samples; it has not
// been compiled here (the SDK is not installed on the development machine).
// Compile errors on a different SDK version are expected to be small API-name
// differences; fix them in this one file.

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cmath>
#include <string>
#include <atomic>
#include "BlackmagicRawAPI.h"

static const char* kLibDirEnv = "BRAW_SDK_LIBS";      // dir holding libBlackmagicRawAPI.so

static std::atomic<int> g_failed{0};
static BlackmagicRawResolutionScale g_scale = blackmagicRawResolutionScaleFull;

class Callback : public IBlackmagicRawCallback {
public:
    virtual void ReadComplete(IBlackmagicRawJob* readJob, HRESULT result, IBlackmagicRawFrame* frame) {
        IBlackmagicRawJob* decodeAndProcess = nullptr;
        if (result == S_OK) result = frame->SetResourceFormat(blackmagicRawResourceFormatRGBU16);
        if (result == S_OK) result = frame->SetResolutionScale(g_scale);
        if (result == S_OK) result = frame->CreateJobDecodeAndProcessFrame(nullptr, nullptr, &decodeAndProcess);
        if (result == S_OK) result = decodeAndProcess->Submit();
        if (result != S_OK) { g_failed = 1; if (decodeAndProcess) decodeAndProcess->Release(); }
        readJob->Release();
    }
    virtual void ProcessComplete(IBlackmagicRawJob* job, HRESULT result, IBlackmagicRawProcessedImage* image) {
        if (result == S_OK) {
            void* data = nullptr; uint32_t bytes = 0;
            image->GetResource(&data);
            image->GetResourceSizeBytes(&bytes);
            if (fwrite(data, 1, bytes, stdout) != bytes) g_failed = 1;
        } else {
            g_failed = 1;
        }
        job->Release();
    }
    virtual void DecodeComplete(IBlackmagicRawJob*, HRESULT) {}
    virtual void TrimProgress(IBlackmagicRawJob*, float) {}
    virtual void TrimComplete(IBlackmagicRawJob*, HRESULT) {}
    virtual void SidecarMetadataParseWarning(IBlackmagicRawClip*, const char*, uint32_t, const char*) {}
    virtual void SidecarMetadataParseError(IBlackmagicRawClip*, const char*, uint32_t, const char*) {}
    virtual void PreparePipelineComplete(void*, HRESULT) {}
    virtual HRESULT STDMETHODCALLTYPE QueryInterface(REFIID, LPVOID*) { return E_NOTIMPL; }
    virtual ULONG STDMETHODCALLTYPE AddRef(void) { return 0; }
    virtual ULONG STDMETHODCALLTYPE Release(void) { return 0; }
};

int main(int argc, char** argv) {
    bool info = false; int scale = 1; const char* clipPath = nullptr;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--info")) info = true;
        else if (!strcmp(argv[i], "--scale") && i + 1 < argc) scale = atoi(argv[++i]);
        else clipPath = argv[i];
    }
    if (!clipPath || (scale != 1 && scale != 2 && scale != 4 && scale != 8)) {
        fprintf(stderr, "usage: braw2pipe [--info] [--scale 1|2|4|8] clip.braw\n");
        return 2;
    }
    const char* libDir = getenv(kLibDirEnv);
    IBlackmagicRawFactory* factory = CreateBlackmagicRawFactoryInstanceFromPath(libDir ? libDir : "");
    if (!factory) { fprintf(stderr, "cannot load the Blackmagic RAW library (set %s)\n", kLibDirEnv); return 3; }
    IBlackmagicRaw* codec = nullptr;
    if (factory->CreateCodec(&codec) != S_OK) { fprintf(stderr, "CreateCodec failed\n"); return 3; }
    IBlackmagicRawClip* clip = nullptr;
    if (codec->OpenClip(clipPath, &clip) != S_OK) { fprintf(stderr, "cannot open %s\n", clipPath); return 4; }

    uint32_t w = 0, h = 0; float fps = 0; uint64_t frames = 0;
    clip->GetWidth(&w); clip->GetHeight(&h); clip->GetFrameRate(&fps); clip->GetFrameCount(&frames);
    w /= scale; h /= scale;
    // BRAW rates are 23.976/24/25/29.97/30/50/59.94/60...: report an exact fraction.
    long num = lroundf(fps * 1001.0f), den = 1001;
    if (fabsf(fps - roundf(fps)) < 0.001f) { num = lroundf(fps); den = 1; }
    if (info) {
        printf("{\"width\": %u, \"height\": %u, \"frames\": %llu, \"fps_num\": %ld, \"fps_den\": %ld, "
               "\"bit_depth\": 12, \"scale\": %d}\n", w, h, (unsigned long long)frames, num, den, scale);
        clip->Release(); codec->Release(); factory->Release();
        return 0;
    }

    Callback cb;
    codec->SetCallback(&cb);
    g_scale = scale == 1 ? blackmagicRawResolutionScaleFull
            : scale == 2 ? blackmagicRawResolutionScaleHalf
            : scale == 4 ? blackmagicRawResolutionScaleQuarter
                         : blackmagicRawResolutionScaleEighth;
    for (uint64_t i = 0; i < frames && !g_failed; i++) {      // in order: one frame in flight
        IBlackmagicRawJob* read = nullptr;
        if (clip->CreateJobReadFrame(i, &read) != S_OK) { g_failed = 1; break; }
        if (read->Submit() != S_OK) { read->Release(); g_failed = 1; break; }
        codec->FlushJobs();
    }
    codec->FlushJobs();
    clip->Release(); codec->Release(); factory->Release();
    fflush(stdout);
    return g_failed ? 5 : 0;
}
