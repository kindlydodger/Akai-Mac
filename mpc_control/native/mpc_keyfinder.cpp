#include <cstdlib>
#include <iostream>
#include <string>
#include <vector>

#include <keyfinder/audiodata.h>
#include <keyfinder/constants.h>
#include <keyfinder/keyfinder.h>
#include <sndfile.h>

namespace {

struct KeyDesc {
    const char* name;
    const char* camelot;
    const char* enum_name;
};

constexpr KeyDesc kKeys[25] = {
    {"A major",  "11B", "A_MAJOR"},
    {"A minor",  "8A",  "A_MINOR"},
    {"Bb major", "6B",  "B_FLAT_MAJOR"},
    {"Bb minor", "3A",  "B_FLAT_MINOR"},
    {"B major",  "1B",  "B_MAJOR"},
    {"B minor",  "10A", "B_MINOR"},
    {"C major",  "8B",  "C_MAJOR"},
    {"C minor",  "5A",  "C_MINOR"},
    {"Db major", "3B",  "D_FLAT_MAJOR"},
    {"Db minor", "12A", "D_FLAT_MINOR"},
    {"D major",  "10B", "D_MAJOR"},
    {"D minor",  "7A",  "D_MINOR"},
    {"Eb major", "5B",  "E_FLAT_MAJOR"},
    {"Eb minor", "2A",  "E_FLAT_MINOR"},
    {"E major",  "12B", "E_MAJOR"},
    {"E minor",  "9A",  "E_MINOR"},
    {"F major",  "7B",  "F_MAJOR"},
    {"F minor",  "4A",  "F_MINOR"},
    {"Gb major", "2B",  "G_FLAT_MAJOR"},
    {"Gb minor", "11A", "G_FLAT_MINOR"},
    {"G major",  "9B",  "G_MAJOR"},
    {"G minor",  "6A",  "G_MINOR"},
    {"Ab major", "4B",  "A_FLAT_MAJOR"},
    {"Ab minor", "1A",  "A_FLAT_MINOR"},
    {"Silence",  "--",  "SILENCE"},
};

}  // namespace

int main(int argc, char** argv) {
    if (argc < 2) {
        std::cerr << "Usage: mpc_keyfinder <audio_file.wav>\n";
        return 2;
    }

    SF_INFO sfinfo{};
    SNDFILE* snd = sf_open(argv[1], SFM_READ, &sfinfo);
    if (!snd) {
        std::cerr << "{\"error\":\"" << sf_strerror(nullptr) << "\"}\n";
        return 1;
    }

    const sf_count_t total_samples = sfinfo.frames * sfinfo.channels;
    std::vector<double> buf(static_cast<size_t>(total_samples));
    const sf_count_t frames_read = sf_readf_double(snd, buf.data(), sfinfo.frames);
    sf_close(snd);

    if (frames_read <= 0) {
        std::cout << "{\"key\":\"Silence\",\"camelot\":\"--\",\"enum\":\"SILENCE\",\"sample_rate\":"
                  << sfinfo.samplerate << ",\"frames\":0}\n";
        return 0;
    }

    KeyFinder::AudioData audio;
    audio.setFrameRate(static_cast<unsigned int>(sfinfo.samplerate));
    audio.setChannels(static_cast<unsigned int>(sfinfo.channels));
    const sf_count_t read_samples = frames_read * sfinfo.channels;
    audio.addToSampleCount(static_cast<unsigned int>(read_samples));
    for (sf_count_t i = 0; i < read_samples; ++i) {
        audio.setSample(static_cast<unsigned int>(i), buf[static_cast<size_t>(i)]);
    }

    KeyFinder::KeyFinder kf;
    const KeyFinder::key_t detected = kf.keyOfAudio(audio);
    const int idx = (detected >= 0 && detected <= 24) ? static_cast<int>(detected) : 24;
    const KeyDesc& kd = kKeys[idx];

    std::cout << "{\"key\":\"" << kd.name
              << "\",\"camelot\":\"" << kd.camelot
              << "\",\"enum\":\"" << kd.enum_name
              << "\",\"sample_rate\":" << sfinfo.samplerate
              << ",\"channels\":" << sfinfo.channels
              << ",\"frames\":" << frames_read << "}\n";
    return 0;
}
