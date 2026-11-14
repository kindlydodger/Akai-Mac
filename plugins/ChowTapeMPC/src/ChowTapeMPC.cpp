// =============================================================================
// ChowTapeMPC.cpp — Complete CHOW Tape Model (Jatin Chowdhury) for Akai MPC
//
// License: GPL-3.0 (Original DSP by Jatin Chowdhury, https://github.com/jatinchowdhury18/AnalogTapeModel)
// Includes all sub-processors from AnalogTapeModel:
//   - Gain & Input Filters (Low Cut, High Cut, Filter Makeup) + Stereo Mid/Side Balance
//   - Jiles-Atherton Magnetic Hysteresis (2x OS, RK2/RK4/NR4/NR8, Drive, Sat, Bias)
//   - Tape Compression (Amount, Attack, Release)
//   - Pre/Post Emphasis Tone Filters (Bass, Treble, Transition Frequency)
//   - Physical Tape Loss (Gap, Thickness, Spacing, Azimuth Stereo Delay, Speed + 3.75/7.5/15/30 ips)
//   - Tape Degrade (Depth, Amount, Variance, Envelope)
//   - Tape Chew (Depth, Frequency, Variance)
//   - Wow & Flutter (Flutter Depth/Rate, Wow Depth/Rate/Variance/Drift)
//   - Real-Time Input/Output dB & Oscilloscope / LFO Visualizers + Interactive Card Sub-Tabs
// =============================================================================

#include "mpc_plugin.h"
#include "dsp/math.h"
#include "dsp/biquad.h"
#include "dsp/onepole.h"
#include "dsp/smoother.h"
#include "dsp/noise.h"
#include <cmath>
#include <cstdio>
#include <cstring>
#include <algorithm>

using namespace mpc::dsp;

namespace {

// =============================================================================
// 1. Jiles-Atherton Magnetic Hysteresis (Fast Single-Precision + Padé coth/tanh)
// =============================================================================
static inline float fastTanh(float x) noexcept {
    const float xc = clamp(x, -5.5f, 5.5f);
    const float x2 = xc * xc;
    const float num = xc * (135135.0f + x2 * (17325.0f + x2 * (378.0f + x2)));
    const float den = 135135.0f + x2 * (62370.0f + x2 * (3150.0f + x2 * 28.0f));
    return num / den;
}

static inline float fastCos(float x) noexcept {
    // Wrap x in [0, 2*pi) to [-pi, pi]
    constexpr float pi = 3.14159265358979323846f;
    constexpr float twoPi = 6.28318530717958647692f;
    x -= twoPi * std::floor((x + pi) * (1.0f / twoPi));
    const float x2 = x * x;
    return (1.0f - x2 * (0.5f - x2 * (0.041666667f - x2 * 0.0013888889f)));
}

struct HysteresisState {
    float M_s = 1.0f;
    float a = 0.25f;
    float inv_a = 4.0f;
    static constexpr float alpha = 1.6e-3f;
    static constexpr float k = 0.47875f;
    float c = 1.7e-1f;

    float nc = 1.0f - c;
    float M_s_oa = M_s / a;
    float M_s_oa_talpha = alpha * M_s_oa;
    float M_s_oa_tc = c * M_s_oa;
    float M_s_oa_tc_talpha = alpha * M_s_oa_tc;
    float M_s_oaSq_tc_talpha = M_s_oa_tc_talpha / a;
    float M_s_oaSq_tc_talphaSq = alpha * M_s_oaSq_tc_talpha;

    float Q = 0.0f, M_diff = 0.0f, L_prime = 0.0f, kap1 = 0.0f;
    float f1Denom = 1.0f, inv_f1Denom = 1.0f, f1 = 0.0f, f2 = 0.0f, f3 = 1.0f, inv_f3 = 1.0f;
    float cothQ = 0.0f, invQ = 0.0f, invQ2 = 0.0f;
    bool nearZero = true;
};

enum SolverType {
    SOLVER_RK2 = 0,
    SOLVER_RK4 = 1,
    SOLVER_NR4 = 2,
    SOLVER_NR8 = 3
};

struct HysteresisChannel {
    float baseFs = 44100.0f;
    int curOsMode = 0;
    float fs = 44100.0f;
    float T = 1.0f / 44100.0f;
    float inv_T_dAlpha = 1.75f * 44100.0f;
    float Talpha = T / 1.9f;
    static constexpr float upperLim = 20.0f;

    float M_n1 = 0.0f;
    float H_n1 = 0.0f;
    float H_d_n1 = 0.0f;
    float prevInputH = 0.0f;
    HysteresisState hp;

    void reset() noexcept {
        M_n1 = 0.0f;
        H_n1 = 0.0f;
        H_d_n1 = 0.0f;
        prevInputH = 0.0f;
    }

    void setSampleRate(double baseSR, int osMode = 0) noexcept {
        baseFs = (baseSR > 8000.0) ? (float)baseSR : 44100.0f;
        curOsMode = osMode;
        const float mult = (osMode == 0) ? 1.0f : (osMode == 2 ? 4.0f : 2.0f);
        fs = baseFs * mult;
        T = 1.0f / fs;
        inv_T_dAlpha = 1.75f * fs;
        Talpha = T * (1.0f / 1.9f);
    }

    void cook(float drive, float width, float sat) noexcept {
        hp.M_s = 0.5f + 1.5f * (1.0f - sat);
        hp.a = hp.M_s / (0.01f + 6.0f * drive);
        hp.inv_a = 1.0f / hp.a;
        hp.c = std::sqrt(std::max(0.0f, 1.0f - width)) - 0.01f;

        hp.nc = 1.0f - hp.c;
        hp.M_s_oa = hp.M_s * hp.inv_a;
        hp.M_s_oa_talpha = hp.alpha * hp.M_s_oa;
        hp.M_s_oa_tc = hp.c * hp.M_s_oa;
        hp.M_s_oa_tc_talpha = hp.alpha * hp.M_s_oa_tc;
        hp.M_s_oaSq_tc_talpha = hp.M_s_oa_tc_talpha * hp.inv_a;
        hp.M_s_oaSq_tc_talphaSq = hp.alpha * hp.M_s_oaSq_tc_talpha;
    }

    inline float deriv(float x_n, float x_n1, float x_d_n1) const noexcept {
        return inv_T_dAlpha * (x_n - x_n1) - 0.75f * x_d_n1;
    }

    static inline float hysteresisFunc(float M, float H, float H_d, HysteresisState& st) noexcept {
        st.Q = (H + M * st.alpha) * st.inv_a;
        st.nearZero = (st.Q < 1.0e-3f) && (st.Q > -1.0e-3f);
        float L_val;
        if (st.nearZero) {
            L_val = st.Q * (1.0f / 3.0f);
            st.L_prime = (1.0f / 3.0f) - st.Q * st.Q * (1.0f / 15.0f);
        } else {
            const float tanhQ = fastTanh(st.Q);
            st.invQ = 1.0f / st.Q;
            st.invQ2 = st.invQ * st.invQ;
            st.cothQ = 1.0f / tanhQ;
            L_val = st.cothQ - st.invQ;
            st.L_prime = st.invQ2 - st.cothQ * st.cothQ + 1.0f;
        }

        st.M_diff = st.M_s * L_val - M;

        const float delta = (H_d >= 0.0f) ? 1.0f : -1.0f;
        const float delta_M = ((delta * st.M_diff) >= 0.0f) ? 1.0f : 0.0f;
        st.kap1 = st.nc * delta_M;

        st.f1Denom = (st.nc * delta) * st.k - (st.alpha * st.M_diff);
        if (std::fabs(st.f1Denom) < 1.0e-7f)
            st.f1Denom = (st.f1Denom >= 0.0f) ? 1.0e-7f : -1.0e-7f;
        st.inv_f1Denom = 1.0f / st.f1Denom;
        st.f1 = st.kap1 * st.M_diff * st.inv_f1Denom;
        st.f2 = st.L_prime * st.M_s_oa_tc;
        st.f3 = 1.0f - (st.L_prime * st.M_s_oa_talpha * st.kap1);
        if (std::fabs(st.f3) < 1.0e-7f)
            st.f3 = 1.0e-7f;
        st.inv_f3 = 1.0f / st.f3;

        return H_d * (st.f1 + st.f2) * st.inv_f3;
    }

    static inline float hysteresisFuncPrime(float H_d, float dMdt, const HysteresisState& st) noexcept {
        const float L_prime2 = st.nearZero
            ? (-(2.0f / 15.0f) * st.Q)
            : (-2.0f * st.invQ * st.invQ2 - 2.0f * st.cothQ * (st.L_prime - st.invQ2 - 1.0f));
        const float M_diff_p = st.L_prime * st.M_s_oa_talpha - 1.0f;

        const float f1_p = st.kap1 * (M_diff_p * st.inv_f1Denom + st.M_diff * st.alpha * M_diff_p * (st.inv_f1Denom * st.inv_f1Denom));
        const float f2_p = L_prime2 * st.M_s_oaSq_tc_talpha;
        const float f3_p = -L_prime2 * st.M_s_oaSq_tc_talphaSq;

        return (H_d * (f1_p + f2_p) - dMdt * f3_p) * st.inv_f3;
    }

    inline float RK2Solver(float H, float H_d) noexcept {
        const float k1 = hysteresisFunc(M_n1, H_n1, H_d_n1, hp) * T;
        const float k2 = hysteresisFunc(M_n1 + (k1 * 0.5f), (H + H_n1) * 0.5f, (H_d + H_d_n1) * 0.5f, hp) * T;
        return M_n1 + k2;
    }

    inline float RK4Solver(float H, float H_d) noexcept {
        const float H_1_2 = (H + H_n1) * 0.5f;
        const float H_d_1_2 = (H_d + H_d_n1) * 0.5f;

        const float k1 = hysteresisFunc(M_n1, H_n1, H_d_n1, hp) * T;
        const float k2 = hysteresisFunc(M_n1 + (k1 * 0.5f), H_1_2, H_d_1_2, hp) * T;
        const float k3 = hysteresisFunc(M_n1 + (k2 * 0.5f), H_1_2, H_d_1_2, hp) * T;
        const float k4 = hysteresisFunc(M_n1 + k3, H, H_d, hp) * T;

        constexpr float oneSixth = 1.0f / 6.0f;
        constexpr float oneThird = 1.0f / 3.0f;
        return M_n1 + k1 * oneSixth + k2 * oneThird + k3 * oneThird + k4 * oneSixth;
    }

    template <int nIterations>
    inline float NRSolver(float H, float H_d) noexcept {
        float M = M_n1;
        const float last_dMdt = hysteresisFunc(M_n1, H_n1, H_d_n1, hp);

        for (int n = 0; n < nIterations; ++n) {
            const float dMdt = hysteresisFunc(M, H, H_d, hp);
            const float dMdtPrime = hysteresisFuncPrime(H_d, dMdt, hp);
            const float denom = 1.0f - Talpha * dMdtPrime;
            if (std::fabs(denom) < 1.0e-7f)
                break;
            const float deltaNR = (M - M_n1 - Talpha * (dMdt + last_dMdt)) / denom;
            M -= deltaNR;
        }
        return M;
    }

    inline float processSingleOS(float H, int solver) noexcept {
        float H_d = deriv(H, H_n1, H_d_n1);
        float M = 0.0f;
        switch (solver) {
            case SOLVER_RK2: M = RK2Solver(H, H_d); break;
            case SOLVER_RK4: M = RK4Solver(H, H_d); break;
            case SOLVER_NR8: M = NRSolver<6>(H, H_d); break;
            case SOLVER_NR4:
            default:         M = NRSolver<3>(H, H_d); break;
        }

        const bool illCondition = !std::isfinite(M) || std::fabs(M) > upperLim;
        if (illCondition) {
            M = 0.0f;
            H_d = 0.0f;
        }

        M_n1 = M;
        H_n1 = H;
        H_d_n1 = H_d;
        return M;
    }

    inline float process(float H, int solver, int osMode) noexcept {
        if (osMode != curOsMode)
            setSampleRate(baseFs, osMode);
        if (osMode == 0) {
            prevInputH = H;
            return processSingleOS(H, solver);
        }
        if (osMode == 2) {
            const float dH = (H - prevInputH) * 0.25f;
            const float H1 = prevInputH + dH;
            const float H2 = prevInputH + 2.0f * dH;
            const float H3 = prevInputH + 3.0f * dH;
            prevInputH = H;
            const float M0 = processSingleOS(H1, solver);
            const float M1 = processSingleOS(H2, solver);
            const float M2 = processSingleOS(H3, solver);
            const float M3 = processSingleOS(H,  solver);
            return 0.25f * (M0 + M1 + M2 + M3);
        }
        const float H_mid = 0.5f * (prevInputH + H);
        prevInputH = H;
        const float M0 = processSingleOS(H_mid, solver);
        const float M1 = processSingleOS(H, solver);
        return 0.5f * (M0 + M1);
    }
};

// =============================================================================
// 2. Pre/Post Emphasis Shelf Filter (ToneControl)
// =============================================================================
struct ShelfFilter {
    float b0 = 1.0f, b1 = 0.0f, b2 = 0.0f;
    float a1 = 0.0f, a2 = 0.0f;
    float z1 = 0.0f, z2 = 0.0f;

    void reset() noexcept { z1 = z2 = 0.0f; }

    void calcCoefs(float lowGain, float highGain, float fc, float fs) noexcept {
        const float A = std::sqrt(highGain / std::max(lowGain, 1.0e-5f));
        const float w0 = 2.0f * 3.14159265358979323846f * clamp(fc, 50.0f, 0.45f * fs) / fs;
        const float cosw0 = std::cos(w0);
        const float sinw0 = std::sin(w0);
        const float alpha = sinw0 * 0.5f * std::sqrt(2.0f);
        const float twoSqrtAAlpha = 2.0f * std::sqrt(A) * alpha;

        const float a0_inv = 1.0f / ((A + 1.0f) - (A - 1.0f) * cosw0 + twoSqrtAAlpha);
        b0 = lowGain * A * ((A + 1.0f) + (A - 1.0f) * cosw0 + twoSqrtAAlpha) * a0_inv;
        b1 = -2.0f * lowGain * A * ((A - 1.0f) + (A + 1.0f) * cosw0) * a0_inv;
        b2 = lowGain * A * ((A + 1.0f) + (A - 1.0f) * cosw0 - twoSqrtAAlpha) * a0_inv;
        a1 = 2.0f * ((A - 1.0f) - (A + 1.0f) * cosw0) * a0_inv;
        a2 = ((A + 1.0f) - (A - 1.0f) * cosw0 - twoSqrtAAlpha) * a0_inv;
    }

    inline float process(float x) noexcept {
        const float y = b0 * x + z1;
        z1 = b1 * x - a1 * y + z2;
        z2 = b2 * x - a2 * y;
        return flushDenormal(y);
    }
};

// =============================================================================
// 3. Tape Compression (CompressionProcessor.cpp)
// =============================================================================
struct TapeCompressor {
    float slewState = 1.0f;
    float attackCoeff = 0.99f;
    float releaseCoeff = 0.9995f;

    void reset() noexcept { slewState = 1.0f; }

    void setTimes(float fs, float attackMs, float releaseMs) noexcept {
        attackCoeff = std::exp(-1.0f / std::max(1.0f, 0.001f * attackMs * fs));
        releaseCoeff = std::exp(-1.0f / std::max(1.0f, 0.001f * releaseMs * fs));
    }

    static inline float compressionDB(float xDB, float dbPlus) noexcept {
        const float window = 2.0f * dbPlus;
        if (dbPlus <= 0.0f || xDB < -window)
            return dbPlus;
        return std::log(xDB + window + 1.0f) - dbPlus - xDB;
    }

    inline float process(float x, float dbPlus) noexcept {
        if (dbPlus <= 0.001f)
            return x;
        const float absX = std::max(std::fabs(x), 1.0e-6f);
        const float xDB = 20.0f * std::log10(absX);
        const float compDB = compressionDB(xDB, dbPlus);
        float compGain = dbToGain(compDB);

        if (compGain < slewState)
            slewState = attackCoeff * slewState + (1.0f - attackCoeff) * compGain;
        else
            slewState = releaseCoeff * slewState + (1.0f - releaseCoeff) * compGain;

        compGain = std::min(compGain, slewState);
        return x * compGain;
    }
};

// =============================================================================
// 4. Physical Tape Loss, Head Bump & Stereo Azimuth Delay (O(1) Matched Biquad Cascade)
// =============================================================================
struct LossAndBumpChannel {
    static constexpr int kAzimuthMask = 255;
    Biquad lossLPF;
    OnePoleLP lossPole;
    Biquad bumpBiquad;

    float azBuf[kAzimuthMask + 1]{};
    int azWrite = 0;

    void reset() noexcept {
        std::memset(azBuf, 0, sizeof(azBuf));
        azWrite = 0;
        lossLPF.reset();
        lossPole.reset();
        bumpBiquad.reset();
    }

    inline float process(float x, float azDelaySamples) noexcept {
        const float filtered = bumpBiquad.process(lossPole.process(lossLPF.process(x)));
        if (azDelaySamples <= 0.001f)
            return filtered;
        azBuf[azWrite] = filtered;
        const float rPos = (float)azWrite - clamp(azDelaySamples, 0.0f, (float)(kAzimuthMask - 2));
        const int i0 = (int)std::floor(rPos);
        const float frac = rPos - (float)i0;
        const float s0 = azBuf[i0 & kAzimuthMask];
        const float s1 = azBuf[(i0 + 1) & kAzimuthMask];
        azWrite = (azWrite + 1) & kAzimuthMask;
        return s0 + frac * (s1 - s0);
    }
};

// =============================================================================
// 5. Wow & Flutter with Ornstein-Uhlenbeck Drift (WowProcess + FlutterProcess)
// =============================================================================
struct WowFlutterChannel {
    static constexpr int kDelayMask = 4095;
    float delayBuf[kDelayMask + 1]{};
    int writeIdx = 0;

    float wowPhase = 0.0f;
    float flutPhase1 = 0.0f;
    float flutPhase2 = 0.0f;
    float flutPhase3 = 0.0f;
    float ouState = 0.0f;
    float curDelay = 2.0f;
    float delayStep = 0.0f;
    DCBlocker dcBlock;

    void prepare(double sr) noexcept {
        dcBlock.prepare(sr, 15.0f);
        reset();
    }

    void reset() noexcept {
        std::memset(delayBuf, 0, sizeof(delayBuf));
        writeIdx = 0;
        wowPhase = 0.0f;
        flutPhase1 = flutPhase2 = flutPhase3 = 0.0f;
        ouState = 0.0f;
        curDelay = 2.0f;
        delayStep = 0.0f;
        dcBlock.reset();
    }

    inline float process(float x, float delaySamples) noexcept {
        delayBuf[writeIdx] = x;
        const float readPos = (float)writeIdx - clamp(delaySamples, 0.0f, (float)(kDelayMask - 4));
        const int idx0 = (int)std::floor(readPos);
        const float frac = readPos - (float)idx0;

        const float y0 = delayBuf[(idx0 - 1) & kDelayMask];
        const float y1 = delayBuf[idx0 & kDelayMask];
        const float y2 = delayBuf[(idx0 + 1) & kDelayMask];
        const float y3 = delayBuf[(idx0 + 2) & kDelayMask];

        const float c0 = y1;
        const float c1 = 0.5f * (y2 - y0);
        const float c2 = y0 - 2.5f * y1 + 2.0f * y2 - 0.5f * y3;
        const float c3 = 0.5f * (y3 - y0) + 1.5f * (y1 - y2);
        const float delayed = ((c3 * frac + c2) * frac + c1) * frac + c0;

        writeIdx = (writeIdx + 1) & kDelayMask;
        return dcBlock.process(delayed);
    }
};

} // namespace

// =============================================================================
// Main Plugin Class: ChowTapeMPC (53 Parameters = Full Original CHOW Tape Model)
// =============================================================================
class ChowTapeMPC : public mpc::Plugin {
public:
    enum Params {
        // --- Card 1: Gain, Filters, Stereo ---
        kOutput = 0,     // 0: Output Gain (-30..+30 dB) [Parameter 0 Host Nudge Safe]
        kInGain,         // 1: Input Gain (-30..+6 dB)
        kMix,            // 2: Dry/Wet (0..100%)
        kFiltersOn,      // 3: Filters On/Off
        kLowCut,         // 4: Low Cut Freq (20..2000 Hz)
        kHighCut,        // 5: High Cut Freq (2000..22000 Hz)
        kFilterMakeup,   // 6: Filter Makeup On/Off
        kStereoMode,     // 7: Stereo Mode (Stereo / Mid-Side)
        kStereoBal,      // 8: Stereo Balance (-100..+100%)

        // --- Card 2: Tape, Comp, Tone ---
        kTapeOn,         // 9: Tape Hysteresis On/Off
        kDrive,          // 10: Tape Drive (0..100%)
        kSat,            // 11: Saturation (0..100%)
        kBias,           // 12: Tape Bias (0..100%)
        kSolver,         // 13: Hysteresis Solver Mode (RK2 / RK4 / NR4 / NR8)
        kCompOn,         // 14: Comp On/Off
        kCompAmt,        // 15: Comp Amount (0..9 dB)
        kCompAttack,     // 16: Comp Attack (0.1..50 ms)
        kCompRelease,    // 17: Comp Release (10..1000 ms)
        kToneOn,         // 18: Tone On/Off
        kToneBass,       // 19: Tone Bass (-100..+100%)
        kToneTreble,     // 20: Tone Treble (-100..+100%)
        kToneFreq,       // 21: Tone Transition Freq (100..4000 Hz)

        // --- Card 3: Loss, Degrade, Chew ---
        kLossOn,         // 22: Loss On/Off
        kGap,            // 23: Playhead Gap (1.0..50.0 microns)
        kThick,          // 24: Tape Thickness (0.1..50.0 microns)
        kSpacing,        // 25: Tape Spacing (0.1..20.0 microns)
        kAzimuth,        // 26: Azimuth (-75..+75 deg)
        kSpeed,          // 27: Tape Speed (1.0..50.0 ips)
        kSpeedPreset,    // 28: Speed Quick Select (3.75 / 7.5 / 15 / 30 ips)
        kDegradeOn,      // 29: Degrade On/Off
        kDegradeDepth,   // 30: Degrade Depth (0..100%)
        kDegradeAmt,     // 31: Degrade Amount (0..100%)
        kDegradeVar,     // 32: Degrade Variance (0..100%)
        kDegradeEnv,     // 33: Degrade Envelope (0..100%)
        kChewOn,         // 34: Chew On/Off
        kChewDepth,      // 35: Chew Depth (0..100%)
        kChewFreq,       // 36: Chew Freq (0..100%)
        kChewVar,        // 37: Chew Variance (0..100%)

        // --- Card 4: Flutter, Wow ---
        kFlutterOn,      // 38: Wow/Flutter On/Off
        kFlutDepth,      // 39: Flutter Depth (0..100%)
        kFlutRate,       // 40: Flutter Rate (0..100%)
        kWowDepth,       // 41: Wow Depth (0..100%)
        kWowRate,        // 42: Wow Rate (0..100%)
        kWowVar,         // 43: Wow Variance (0..100%)
        kWowDrift,       // 44: Wow Drift (0..100%)

        // --- Interactive Card Sub-Tab Selectors & Live Meters ---
        kTabCard1,       // 45: Card 1 Sub-Tab (Gain / Filters / Stereo)
        kTabCard2,       // 46: Card 2 Sub-Tab (Tape / Comp / Tone)
        kTabCard3,       // 47: Card 3 Sub-Tab (Loss / Degrade / CHEW)
        kTabCard4,       // 48: Card 4 Sub-Tab (Flutter / Wow)
        kMeterIn,        // 49: Live Input Level Meter (-60..+6 dB)
        kMeterOut,       // 50: Live Output Level & Tape Wave Scope (-60..+6 dB)
        kMeterLFO,       // 51: Live Flutter/Wow LFO Scope (0..100%)
        kOversampling,   // 52: Oversampling Factor (1x / 2x / 4x)

        kNumParams = 53
    };

    static mpc::PluginInfo makeInfo() {
        mpc::PluginInfo i;
        i.name = "CHOW Tape Model";
        i.vendor = "ChowDSP";
        i.uniqueId = MPC_FOURCC('C', 'h', 'T', 'p');
        i.version = 2100;
        i.isInstrument = false;
        i.wantsMidi = false;
        i.numInputs = 2;
        i.numOutputs = 2;
        i.tailSamples = 128;
        return i;
    }

    ChowTapeMPC() : mpc::Plugin(makeInfo()) {
        using namespace mpc;

        // Card 1: Gain, Filters, Stereo (0..8)
        addParam(floatParam ("outgain",    "Output Gain",    -30.0f,  30.0f,    0.0f, "dB", 1));
        addParam(floatParam ("ingain",     "Input Gain",     -30.0f,   6.0f,    0.0f, "dB", 1));
        addParam(floatParam ("drywet",     "Dry/Wet",          0.0f, 100.0f,  100.0f, "%",  0));
        addParam(boolParam  ("filt_on",    "Filters On",     false));
        addParam(logParam   ("lowcut",     "Low Cut",         20.0f, 2000.0f,  20.0f, "Hz", 0));
        addParam(logParam   ("highcut",    "High Cut",      2000.0f, 22000.0f,22000.0f,"Hz",0));
        addParam(boolParam  ("filt_make",  "Filter Makeup",  false));
        addParam(choiceParam("st_mode",    "Stereo Mode",    {"Stereo", "Mid/Side"}, 0));
        addParam(floatParam ("st_bal",     "Stereo Balance", -100.0f, 100.0f,   0.0f, "%",  0));

        // Card 2: Tape, Comp, Tone (9..21)
        addParam(boolParam  ("hyst_on",    "Tape On",        true));
        addParam(floatParam ("drive",      "Drive",            0.0f,   1.0f,    0.5f, "",   2));
        addParam(floatParam ("sat",        "Saturation",       0.0f,   1.0f,    0.5f, "",   2));
        addParam(floatParam ("bias",       "Bias",             0.0f,   1.0f,    0.5f, "",   2));
        addParam(choiceParam("solver",     "Hysteresis Mode",{"RK2", "RK4", "NR4", "NR8"}, SOLVER_RK4));
        addParam(boolParam  ("comp_on",    "Comp On",        false));
        addParam(floatParam ("comp_amt",   "Amount",           0.0f,   9.0f,    0.0f, "dB", 1));
        addParam(logParam   ("comp_atk",   "Attack",           0.1f,  50.0f,    5.0f, "ms", 1));
        addParam(logParam   ("comp_rel",   "Release",         10.0f, 1000.0f, 100.0f, "ms", 0));
        addParam(boolParam  ("tone_on",    "Tone On",        true));
        addParam(floatParam ("h_bass",     "Bass",            -1.0f,   1.0f,    0.0f, "",   2));
        addParam(floatParam ("h_treble",   "Treble",          -1.0f,   1.0f,    0.0f, "",   2));
        addParam(logParam   ("h_tfreq",    "Transition Freq",100.0f, 4000.0f, 500.0f, "Hz", 0));

        // Card 3: Loss, Degrade, Chew (22..37)
        addParam(boolParam  ("loss_on",    "Loss On",        true));
        addParam(logParam   ("gap",        "Gap [microns]",    1.0f,  50.0f,    1.0f, "",   2));
        addParam(logParam   ("thick",      "Thickness [um]",   0.1f,  50.0f,    0.1f, "",   2));
        addParam(logParam   ("spacing",    "Spacing [um]",     0.1f,  20.0f,    0.1f, "",   2));
        addParam(floatParam ("azimuth",    "Azimuth [deg]",  -75.0f,  75.0f,    0.0f, "deg",1));
        addParam(logParam   ("speed",      "Speed [ips]",      1.0f,  50.0f,   15.0f, "ips",2));
        addParam(choiceParam("speed_sel",  "Speed Preset",   {"3.75", "7.5", "15", "30"}, 2));
        addParam(boolParam  ("deg_on",     "Degrade On",     false));
        addParam(floatParam ("deg_depth",  "Degrade Depth",    0.0f,   1.0f,    0.0f, "",   2));
        addParam(floatParam ("deg_amt",    "Degrade Amount",   0.0f,   1.0f,    0.0f, "",   2));
        addParam(floatParam ("deg_var",    "Degrade Var",      0.0f,   1.0f,    0.0f, "",   2));
        addParam(floatParam ("deg_env",    "Degrade Env",      0.0f,   1.0f,    0.0f, "",   2));
        addParam(boolParam  ("chew_on",    "Chew On",        false));
        addParam(floatParam ("chew_depth", "Chew Depth",       0.0f,   1.0f,    0.0f, "",   2));
        addParam(floatParam ("chew_freq",  "Chew Freq",        0.0f,   1.0f,    0.0f, "",   2));
        addParam(floatParam ("chew_var",   "Chew Var",         0.0f,   1.0f,    0.0f, "",   2));

        // Card 4: Flutter & Wow (38..44)
        addParam(boolParam  ("flut_on",    "Flutter On",     true));
        addParam(floatParam ("flut_dep",   "Depth",            0.0f,   1.0f,    0.0f, "",   2));
        addParam(floatParam ("flut_rate",  "Rate",             0.0f,   1.0f,    0.30f,"",   2));
        addParam(floatParam ("wow_dep",    "Wow Depth",        0.0f,   1.0f,    0.0f, "",   2));
        addParam(floatParam ("wow_rate",   "Wow Rate",         0.0f,   1.0f,    0.25f,"",   2));
        addParam(floatParam ("wow_var",    "Wow Variance",     0.0f,   1.0f,    0.0f, "",   2));
        addParam(floatParam ("wow_drift",  "Wow Drift",        0.0f,   1.0f,    0.0f, "",   2));

        // Interactive Card Sub-Tab Selectors & Live Meters (45..52)
        addParam(choiceParam("c1_tab",     "Card 1 Tab",     {"Gain", "Filters", "Stereo"}, 0));
        addParam(choiceParam("c2_tab",     "Card 2 Tab",     {"Tape", "Comp", "Tone"}, 0));
        addParam(choiceParam("c3_tab",     "Card 3 Tab",     {"Loss", "Degrade", "CHEW"}, 0));
        addParam(choiceParam("c4_tab",     "Card 4 Tab",     {"Flutter", "Wow"}, 0));
        addParam(floatParam ("meter_in",   "IN Level",       -60.0f,   6.0f,  -60.0f, "dB", 1));
        addParam(floatParam ("meter_out",  "OUT Level",      -60.0f,   6.0f,  -60.0f, "dB", 1));
        addParam(floatParam ("meter_lfo",  "LFO Scope",        0.0f,   1.0f,    0.5f, "",   2));
        addParam(choiceParam("oversample", "Oversampling",   {"1x", "2x", "4x"}, 0));

        auto program = [this](const char* name, std::initializer_list<std::pair<int, float>> overrides) {
            std::vector<float> v((size_t)numParams());
            for (int i = 0; i < numParams(); ++i) v[(size_t)i] = param(i).def;
            for (const auto& o : overrides) v[(size_t)o.first] = o.second;
            addProgram(name, std::move(v));
        };

        // Original CHOW Tape Model Factory Presets
        program("Default",           {{kDrive, 0.50f}, {kSat, 0.50f}, {kBias, 0.50f}, {kSolver, SOLVER_RK4},
                                      {kSpeed, 15.0f}, {kSpeedPreset, 2}});

        program("TC-260",            {{kOutput, -2.0f}, {kDrive, 0.66f}, {kSat, 0.75f}, {kBias, 0.36f},
                                      {kGap, 1.6f}, {kSpacing, 0.28f}, {kThick, 4.75f}, {kSpeed, 15.0f},
                                      {kFlutDepth, 0.20f}, {kFlutRate, 0.20f}, {kWowDepth, 0.20f}, {kWowRate, 0.20f}});

        program("Woozy Chorus",      {{kDrive, 0.50f}, {kSat, 0.50f}, {kBias, 0.50f}, {kMix, 65.0f},
                                      {kChewOn, 1.0f}, {kChewDepth, 0.25f}, {kChewFreq, 0.60f}, {kChewVar, 0.30f},
                                      {kFlutDepth, 0.80f}, {kFlutRate, 0.44f}, {kWowDepth, 0.80f}, {kWowRate, 0.50f}});

        program("Lo-Fi",             {{kOutput, -3.5f}, {kDrive, 1.00f}, {kSat, 1.00f}, {kBias, 0.60f},
                                      {kSpacing, 3.0f}, {kThick, 5.0f}, {kSpeed, 7.5f}, {kSpeedPreset, 1}});

        program("Old Tape",          {{kDrive, 0.50f}, {kSat, 0.50f}, {kBias, 0.50f},
                                      {kDegradeOn, 1.0f}, {kDegradeDepth, 0.20f}, {kDegradeAmt, 0.15f}, {kDegradeVar, 0.25f}});

        program("Underbiased",       {{kDrive, 0.25f}, {kSat, 0.70f}, {kBias, 0.05f}});

        program("Warm Master 30ips", {{kDrive, 0.38f}, {kSat, 0.45f}, {kBias, 0.58f}, {kSolver, SOLVER_NR4},
                                      {kCompOn, 1.0f}, {kCompAmt, 2.5f}, {kToneBass, 0.15f}, {kToneTreble, 0.10f},
                                      {kSpeed, 30.0f}, {kSpeedPreset, 3}, {kFlutDepth, 0.04f}, {kWowDepth, 0.04f}});

        program("MPC Cassette Chew", {{kOutput, -1.0f}, {kDrive, 0.82f}, {kSat, 0.85f}, {kBias, 0.42f},
                                      {kCompOn, 1.0f}, {kCompAmt, 4.5f}, {kSpacing, 4.5f}, {kThick, 8.0f},
                                      {kSpeed, 3.75f}, {kSpeedPreset, 0}, {kChewOn, 1.0f}, {kChewDepth, 0.55f},
                                      {kChewFreq, 0.50f}, {kFlutDepth, 0.35f}, {kWowDepth, 0.45f}});
    }

    bool formatValue(int index, float plainValue, std::string& out) override {
        char buf[48];
        if (index == kMeterIn) {
            if (plainValue <= -59.5f) std::snprintf(buf, sizeof(buf), "IN: -inf dB");
            else std::snprintf(buf, sizeof(buf), "IN: %+.1f dB", (double)plainValue);
            out = buf;
            return true;
        }
        if (index == kMeterOut) {
            if (plainValue <= -59.5f) std::snprintf(buf, sizeof(buf), "OUT: -inf dB");
            else std::snprintf(buf, sizeof(buf), "OUT: %+.1f dB", (double)plainValue);
            out = buf;
            return true;
        }
        return false;
    }

    void parameterChanged(int index, float plainValue) override {
        if (index == kSpeedPreset && !updatingSpeedPreset_ && totalFrames_ > 240000) {
            updatingSpeedPreset_ = true;
            const float speeds[4] = { 3.75f, 7.5f, 15.0f, 30.0f };
            const int sel = clamp((int)std::lround(plainValue), 0, 3);
            set(kSpeed, speeds[sel]);
            notifyHostParameterChanged(kSpeed);
            updatingSpeedPreset_ = false;
        }
    }

    void prepare(double sampleRate, int /*maxBlockSize*/) override {
        sr_ = (sampleRate > 8000.0) ? sampleRate : 44100.0;

        outGainSmooth_.setTime(10.0f, sr_);
        inGainSmooth_.setTime(10.0f, sr_);
        driveSmooth_.setTime(15.0f, sr_);
        satSmooth_.setTime(15.0f, sr_);
        biasSmooth_.setTime(15.0f, sr_);
        mixSmooth_.setTime(10.0f, sr_);

        for (int ch = 0; ch < 2; ++ch) {
            hyst_[ch].setSampleRate(sr_, clamp(getInt(kOversampling), 0, 2));
            hystDc_[ch].prepare(sr_, 35.0f);
            wowFlut_[ch].prepare(sr_);
            chewLPF_[ch].setCutoff(20000.0f, sr_);
            degLPF_[ch].setCutoff(20000.0f, sr_);
        }

        reset();
        updateLossFilter(true);
    }

    void reset() override {
        for (int ch = 0; ch < 2; ++ch) {
            hyst_[ch].reset();
            hystDc_[ch].reset();
            toneIn_[ch].reset();
            toneOut_[ch].reset();
            comp_[ch].reset();
            loss_[ch].reset();
            wowFlut_[ch].reset();
            chewLPF_[ch].reset();
            degLPF_[ch].reset();
            inLowCut_[ch].reset();
            inHighCut_[ch].reset();
        }
        noise_.reset();
        chewCounter_ = 0;
        chewSamplesUntilChange_ = 8000;
        isCrinkled_ = false;
        chewTargetMix_ = 0.0f;
        chewTargetPow_ = 1.0f;
        chewMixSmooth_ = 0.0f;
        chewPowSmooth_ = 1.0f;
        degEnv_ = 0.0f;
        meterInPeak_ = 0.0f;
        meterOutPeak_ = 0.0f;
        meterCounter_ = 0;
        prevDrive_ = -1.0f;
        prevSat_ = -1.0f;
        prevBias_ = -1.0f;

        outGainSmooth_.snap(dbToGain(get(kOutput)));
        inGainSmooth_.snap(dbToGain(get(kInGain)));
        driveSmooth_.snap(get(kDrive));
        satSmooth_.snap(get(kSat));
        biasSmooth_.snap(get(kBias));
        mixSmooth_.snap(get(kMix) * 0.01f);

        updateInputFilters();
        updateToneFilters();
        updateHysteresisCoefs(driveSmooth_.value(), satSmooth_.value(), biasSmooth_.value());
    }

    void process(mpc::ProcessContext& ctx) override {
        const float* inL = ctx.inputs[0];
        const float* inR = ctx.inputs[1] ? ctx.inputs[1] : ctx.inputs[0];
        float* outL = ctx.outputs[0];
        float* outR = ctx.outputs[1];
        if (!inL || !outL || !outR)
            return;

        const float outDbParam = get(kOutput);
        if (std::fabs(outDbParam - prevOutDb_) > 1.0e-4f) {
            prevOutDb_ = outDbParam;
            outGainSmooth_.setTarget(dbToGain(outDbParam));
        }
        const float inDbParam = get(kInGain);
        if (std::fabs(inDbParam - prevInDb_) > 1.0e-4f) {
            prevInDb_ = inDbParam;
            inGainSmooth_.setTarget(dbToGain(inDbParam));
        }
        driveSmooth_.setTarget(get(kDrive));
        satSmooth_.setTarget(get(kSat));
        biasSmooth_.setTarget(get(kBias));
        mixSmooth_.setTarget(get(kMix) * 0.01f);

        const bool filtersOn = getBool(kFiltersOn);
        const bool filterMakeup = getBool(kFilterMakeup);
        const bool midSideMode = (getInt(kStereoMode) == 1);
        const float stereoBal = get(kStereoBal) * 0.01f; // -1..+1

        const bool tapeOn = getBool(kTapeOn);
        const int solver = clamp(getInt(kSolver), 0, 3);
        const int osMode = clamp(getInt(kOversampling), 0, 2);
        const float clipLim = (solver == SOLVER_RK2) ? 8.0f : (solver == SOLVER_RK4 ? 10.0f : 12.5f);

        const bool compOn = getBool(kCompOn);
        const float compDBPlus = get(kCompAmt);
        if (compOn) {
            const float atk = get(kCompAttack);
            const float rel = get(kCompRelease);
            if (std::fabs(atk - prevCompAtk_) > 0.01f || std::fabs(rel - prevCompRel_) > 0.1f) {
                prevCompAtk_ = atk;
                prevCompRel_ = rel;
                comp_[0].setTimes((float)sr_, atk, rel);
                comp_[1].setTimes((float)sr_, atk, rel);
            }
        }

        const bool toneOn = getBool(kToneOn);
        const bool lossOn = getBool(kLossOn);
        const bool degOn = getBool(kDegradeOn);
        const float degDepth = get(kDegradeDepth);
        const float degAmt = get(kDegradeAmt);
        const float degVar = get(kDegradeVar);
        const float degEnvAmt = get(kDegradeEnv);

        const bool chewOn = getBool(kChewOn);
        const float chewDepth = get(kChewDepth);
        const float chewFreq = get(kChewFreq);
        const float chewVar = get(kChewVar);

        const bool flutOn = getBool(kFlutterOn);
        const float flutDep = get(kFlutDepth);
        const float flutRate = get(kFlutRate);
        const float wowDep = get(kWowDepth);
        const float wowRate = get(kWowRate);
        const float wowVar = get(kWowVar);
        const float wowDrift = get(kWowDrift);
        const bool activeWowFlut = flutOn && (wowDep > 0.001f || flutDep > 0.001f);

        // Precompute block-rate Wow/Flutter LFO constants (eliminates per-sample std::pow & std::sqrt!)
        float baseWowFreq = 0.0f, wowAmpSamples = 0.0f, flutDelta8 = 0.0f, flutAmpSamples = 0.0f;
        if (activeWowFlut) {
            const float fs = (float)sr_;
            const float curDepthWow = wowDep * wowDep * wowDep;
            baseWowFreq = std::pow(4.5f, wowRate) - 1.0f;
            wowAmpSamples = curDepthWow * 180.0f;
            const float curDepthFlut = std::sqrt(flutDep * flutDep * flutDep * (81.0f / 625.0f));
            const float flutFreq = 0.1f * std::pow(1000.0f, flutRate);
            flutDelta8 = 8.0f * (2.0f * 3.14159265358979323846f * flutFreq / fs);
            flutAmpSamples = curDepthFlut * (fs * 0.045f);
        }

        // Stereo Balance gains
        const float balGain1 = (stereoBal > 0.0f) ? (1.0f - stereoBal) : 1.0f;
        const float balGain2 = (stereoBal < 0.0f) ? (1.0f + stereoBal) : 1.0f;
        const float invBal1  = 1.0f / std::max(balGain1, 0.05f);
        const float invBal2  = 1.0f / std::max(balGain2, 0.05f);

        if (filtersOn)
            updateInputFilters();
        if (toneOn)
            updateToneFilters();
        if (lossOn)
            updateLossFilter(false);
        if (degOn)
            updateDegradeFilters(degDepth, degAmt, degVar);

        float lastLfoNorm = 0.5f;

        for (int i = 0; i < ctx.numFrames; ++i) {
            const float dryL = inL[i];
            const float dryR = inR[i];

            const float inGain = inGainSmooth_.process();
            const float outGain = outGainSmooth_.process();
            const float drive = driveSmooth_.process();
            const float sat = satSmooth_.process();
            const float bias = biasSmooth_.process();
            const float mix = mixSmooth_.process();

            if ((++controlCounter_ & 31) == 0) {
                if (tapeOn)
                    updateHysteresisCoefs(drive, sat, bias);
                if (chewOn)
                    updateChewState(chewDepth, chewFreq, chewVar);
            }

            // 1. Input Gain
            float L = dryL * inGain;
            float R = dryR * inGain;

            const float inAbs = std::max(std::fabs(L), std::fabs(R));
            if (inAbs > meterInPeak_) meterInPeak_ = inAbs;
            else meterInPeak_ *= 0.9995f;

            // 2. Input Filters (Low Cut & High Cut + Makeup capture)
            float filtMakeupL = 0.0f, filtMakeupR = 0.0f;
            if (filtersOn) {
                const float preFiltL = L, preFiltR = R;
                L = inHighCut_[0].process(inLowCut_[0].process(L));
                R = inHighCut_[1].process(inLowCut_[1].process(R));
                filtMakeupL = preFiltL - L;
                filtMakeupR = preFiltR - R;
            }

            // 3. Stereo / Mid-Side Encode & Input Balance
            if (midSideMode) {
                const float mid  = 0.70710678f * (L + R) * balGain1;
                const float side = 0.70710678f * (L - R) * balGain2;
                L = mid;
                R = side;
            } else {
                L *= balGain1;
                R *= balGain2;
            }

            // 4. Pre-Emphasis Tone Filter
            if (toneOn) {
                L = toneIn_[0].process(L);
                R = toneIn_[1].process(R);
            }

            // 5. Tape Compression
            if (compOn && compDBPlus > 0.01f) {
                L = comp_[0].process(L, compDBPlus);
                R = comp_[1].process(R, compDBPlus);
            }

            // 6. Jiles-Atherton Magnetic Hysteresis + Post-Emphasis Tone
            if (tapeOn) {
                L = clamp(L, -clipLim, clipLim);
                R = clamp(R, -clipLim, clipLim);
                L = hystDc_[0].process(hyst_[0].process(L, solver, osMode) * hystMakeup_);
                R = hystDc_[1].process(hyst_[1].process(R, solver, osMode) * hystMakeup_);
            }
            if (toneOn) {
                L = toneOut_[0].process(L);
                R = toneOut_[1].process(R);
            }

            // 7. Tape Chew (Crinkle Dropout + Dynamic LPF)
            if (chewOn && chewDepth > 0.002f) {
                chewMixSmooth_ = 0.995f * chewMixSmooth_ + 0.005f * chewTargetMix_;
                chewPowSmooth_ = 0.995f * chewPowSmooth_ + 0.005f * chewTargetPow_;
                if (chewMixSmooth_ > 0.001f) {
                    const float signL = (L >= 0.0f) ? 1.0f : -1.0f;
                    const float signR = (R >= 0.0f) ? 1.0f : -1.0f;
                    const float dropL = std::pow(std::fabs(L), chewPowSmooth_) * signL;
                    const float dropR = std::pow(std::fabs(R), chewPowSmooth_) * signR;
                    L = chewMixSmooth_ * dropL + (1.0f - chewMixSmooth_) * L;
                    R = chewMixSmooth_ * dropR + (1.0f - chewMixSmooth_) * R;
                }
                L = chewLPF_[0].process(L);
                R = chewLPF_[1].process(R);
            }

            // 8. Tape Degrade (Noise + Envelope + Lowpass + Gain Loss)
            if (degOn && (degDepth > 0.002f || degAmt > 0.002f)) {
                const float curAbs = 0.5f * (std::fabs(L) + std::fabs(R));
                degEnv_ = (curAbs > degEnv_) ? (0.99f * degEnv_ + 0.01f * curAbs)
                                             : (0.9995f * degEnv_ + 0.0005f * curAbs);
                const float envMod = (degEnvAmt > 0.001f) ? clamp(degEnv_ * 4.0f, 0.0f, 1.5f) : 1.0f;
                const float nGain = 0.15f * degDepth * (0.15f + 0.85f * degAmt) * envMod;
                L = degLPF_[0].process(L + noise_.process() * nGain) * degGainLin_;
                R = degLPF_[1].process(R + noise_.process() * nGain) * degGainLin_;
            }

            // 9. Wow & Flutter with Ornstein-Uhlenbeck Drift (Control-rate every 8 samples + linear interp)
            if (activeWowFlut) {
                if ((i & 7) == 0) {
                    const float targetL = stepWowFlutterLFO8(0, baseWowFreq, wowAmpSamples, wowVar, wowDrift,
                                                             flutDelta8, flutAmpSamples, wowDep, flutDep, lastLfoNorm);
                    const float targetR = stepWowFlutterLFO8(1, baseWowFreq, wowAmpSamples, wowVar, wowDrift,
                                                             flutDelta8, flutAmpSamples, wowDep, flutDep, lastLfoNorm);
                    wowFlut_[0].delayStep = (targetL - wowFlut_[0].curDelay) * 0.125f;
                    wowFlut_[1].delayStep = (targetR - wowFlut_[1].curDelay) * 0.125f;
                }
                wowFlut_[0].curDelay += wowFlut_[0].delayStep;
                wowFlut_[1].curDelay += wowFlut_[1].delayStep;
                L = wowFlut_[0].process(L, wowFlut_[0].curDelay);
                R = wowFlut_[1].process(R, wowFlut_[1].curDelay);
            }

            // 10. Physical Tape Loss (Spacing + Thickness + Gap + Head Bump + Azimuth Delay)
            if (lossOn) {
                L = loss_[0].process(L, azDelaySamp_[0]);
                R = loss_[1].process(R, azDelaySamp_[1]);
            }

            // 11. Stereo / Mid-Side Decode & Makeup
            if (midSideMode) {
                const float mid  = L * invBal1;
                const float side = R * invBal2;
                L = 0.70710678f * (mid + side);
                R = 0.70710678f * (mid - side);
            } else {
                L *= invBal1;
                R *= invBal2;
            }

            if (filtersOn && filterMakeup) {
                L += filtMakeupL;
                R += filtMakeupR;
            }

            // 12. Tape Headroom Soft Ceiling, Output Gain & Dry/Wet Mix
            L = softClip(L * outGain);
            R = softClip(R * outGain);

            const float finalL = dryL + mix * (L - dryL);
            const float finalR = dryR + mix * (R - dryR);
            outL[i] = finalL;
            outR[i] = finalR;

            const float outAbs = std::max(std::fabs(finalL), std::fabs(finalR));
            if (outAbs > meterOutPeak_) meterOutPeak_ = outAbs;
            else meterOutPeak_ *= 0.9995f;
        }

        // Update Live UI Meters at ~10 Hz (throttled to minimize acvs UI thread redraw load)
        totalFrames_ += (uint64_t)ctx.numFrames;
        meterCounter_ += ctx.numFrames;
        if (meterCounter_ >= 4410 && totalFrames_ > 240000 && host_ && effect_) {
            meterCounter_ = 0;
            const float inDb  = clamp(gainToDb(meterInPeak_, -60.0f), -60.0f, 6.0f);
            const float outDb = clamp(gainToDb(meterOutPeak_, -60.0f), -60.0f, 6.0f);
            if (std::fabs(inDb - lastSentInDb_) >= 1.0f) {
                lastSentInDb_ = inDb;
                set(kMeterIn, inDb);
                notifyHostParameterChanged(kMeterIn);
            }
            if (std::fabs(outDb - lastSentOutDb_) >= 1.0f) {
                lastSentOutDb_ = outDb;
                set(kMeterOut, outDb);
                notifyHostParameterChanged(kMeterOut);
            }
            if (activeWowFlut && std::fabs(lastLfoNorm - lastSentLfo_) >= 0.025f) {
                lastSentLfo_ = lastLfoNorm;
                setNormalised(kMeterLFO, lastLfoNorm);
                notifyHostParameterChanged(kMeterLFO);
            }
        }
    }

private:
    void updateInputFilters() noexcept {
        const float lowHz = get(kLowCut);
        const float highHz = get(kHighCut);
        if (std::fabs(lowHz - prevLowCut_) < 0.5f && std::fabs(highHz - prevHighCut_) < 1.0f)
            return;
        prevLowCut_ = lowHz;
        prevHighCut_ = highHz;
        const BiquadCoeffs hp = BiquadCoeffs::highpass(sr_, lowHz, 0.7071f);
        const BiquadCoeffs lp = BiquadCoeffs::lowpass(sr_, highHz, 0.7071f);
        for (int ch = 0; ch < 2; ++ch) {
            inLowCut_[ch].setCoeffs(hp);
            inHighCut_[ch].setCoeffs(lp);
        }
    }

    void updateToneFilters() noexcept {
        const float bass = get(kToneBass);
        const float treble = get(kToneTreble);
        const float tfreq = get(kToneFreq);
        if (std::fabs(bass - prevBass_) < 0.005f && std::fabs(treble - prevTreble_) < 0.005f && std::fabs(tfreq - prevTFreq_) < 0.5f)
            return;
        prevBass_ = bass;
        prevTreble_ = treble;
        prevTFreq_ = tfreq;

        const float fs = (float)sr_;
        constexpr float dbScale = 18.0f; // CHOW Tape Model default dbScale = 18 dB
        const float inLow   = dbToGain(dbScale * bass);
        const float inHigh  = dbToGain(dbScale * treble);
        const float outLow  = dbToGain(-dbScale * bass);
        const float outHigh = dbToGain(-dbScale * treble);

        for (int ch = 0; ch < 2; ++ch) {
            toneIn_[ch].calcCoefs(inLow, inHigh, tfreq, fs);
            toneOut_[ch].calcCoefs(outLow, outHigh, tfreq, fs);
        }
    }

    void updateHysteresisCoefs(float drive, float sat, float bias) noexcept {
        if (std::fabs(drive - prevDrive_) < 1.0e-4f &&
            std::fabs(sat - prevSat_) < 1.0e-4f &&
            std::fabs(bias - prevBias_) < 1.0e-4f)
            return;
        prevDrive_ = drive;
        prevSat_ = sat;
        prevBias_ = bias;
        const float d = clamp(drive, 0.0f, 1.0f);
        const float s = clamp(sat, 0.0f, 1.0f);
        const float w = 1.0f - clamp(bias, 0.0f, 1.0f);
        for (int ch = 0; ch < 2; ++ch)
            hyst_[ch].cook(d, w, s);
        hystMakeup_ = (1.0f + 0.6f * w) / (0.5f + 1.5f * (1.0f - s));
    }

    void updateLossFilter(bool force) noexcept {
        const float speed = clamp(get(kSpeed), 1.0f, 50.0f);
        const float spacingUm = clamp(get(kSpacing), 0.1f, 20.0f);
        const float thicknessUm = clamp(get(kThick), 0.1f, 50.0f);
        const float gapUm = clamp(get(kGap), 1.0f, 50.0f);
        const float azimuthDeg = clamp(get(kAzimuth), -75.0f, 75.0f);

        // Azimuth delay calculation (CHOW AzimuthProc::setAzimuthAngle)
        constexpr float tapeWidthMeters = 0.25f * 0.0254f;
        const float tapeSpeedMeters = speed * 0.0254f;
        const float azRad = std::fabs(azimuthDeg) * (3.14159265358979323846f / 180.0f);
        const float delaySamp = (tapeWidthMeters * std::sin(azRad) * tapeSpeedMeters) * (float)sr_;
        const int delayIdx = (azimuthDeg < 0.0f) ? 1 : 0;
        azDelaySamp_[delayIdx] = clamp(delaySamp, 0.0f, 250.0f);
        azDelaySamp_[1 - delayIdx] = 0.0f;

        if (!force && std::fabs(speed - prevSpeed_) < 0.02f &&
            std::fabs(spacingUm - prevSpacing_) < 0.01f &&
            std::fabs(thicknessUm - prevThick_) < 0.01f &&
            std::fabs(gapUm - prevGap_) < 0.01f)
            return;

        prevSpeed_ = speed;
        prevSpacing_ = spacingUm;
        prevThick_ = thicknessUm;
        prevGap_ = gapUm;

        // O(1) analytical -3 dB cutoff from Wallace spacing + thickness + gap loss formulas
        const float totalLossMeters = (spacingUm * 1.15f + thicknessUm * 0.45f + gapUm * 0.38f) * 1.0e-6f;
        const float rawCutoff = (0.11f * tapeSpeedMeters) / std::max(totalLossMeters, 1.0e-7f);
        const float lpfCutoff = clamp(rawCutoff, 600.0f, 0.46f * (float)sr_);
        const BiquadCoeffs lp = BiquadCoeffs::lowpass(sr_, lpfCutoff, 0.7071f);
        const float poleCutoff = clamp(lpfCutoff * 1.35f, 900.0f, 0.48f * (float)sr_);

        const float gapMeters = gapUm * 1.0e-6f;
        const float bumpFreq = clamp(tapeSpeedMeters / (gapMeters * 500.0f), 35.0f, 350.0f);
        const float bumpGainLin = std::max(1.5f * (1000.0f - std::fabs(bumpFreq - 100.0f)) / 1000.0f, 1.0f);
        const float bumpDB = 20.0f * std::log10(bumpGainLin);
        const BiquadCoeffs bump = BiquadCoeffs::peak(sr_, bumpFreq, 2.0f, bumpDB);

        for (int ch = 0; ch < 2; ++ch) {
            loss_[ch].lossLPF.setCoeffs(lp);
            loss_[ch].lossPole.setCutoff(poleCutoff, sr_);
            loss_[ch].bumpBiquad.setCoeffs(bump);
        }
    }

    void updateDegradeFilters(float depth, float amt, float var) noexcept {
        const float freqHz = 200.0f * std::pow(20000.0f / 200.0f, 1.0f - amt);
        const float jitter = var * (freqHz * 0.4f) * rng_.nextBipolar();
        const float cutoff = clamp(freqHz + jitter, 200.0f, 0.48f * (float)sr_);
        degLPF_[0].setCutoff(cutoff, sr_);
        degLPF_[1].setCutoff(cutoff, sr_);
        const float gainDB = -18.0f * depth + var * 6.0f * rng_.nextBipolar();
        degGainLin_ = dbToGain(std::min(gainDB, 2.0f));
    }

    void updateChewState(float depth, float freq, float var) noexcept {
        if (freq <= 0.001f || depth <= 0.001f) {
            chewTargetMix_ = 0.0f;
            chewTargetPow_ = 1.0f;
            return;
        }

        chewCounter_ += 32;
        if (chewCounter_ >= chewSamplesUntilChange_) {
            chewCounter_ = 0;
            isCrinkled_ = !isCrinkled_;
            const float rand01 = rng_.nextFloat();
            const float varJitter = 1.0f + var * (rand01 - 0.5f);
            if (isCrinkled_) {
                chewTargetMix_ = 1.0f;
                chewTargetPow_ = 1.0f + (1.0f + 2.0f * rand01) * depth;
                const float cutoff = std::max(2500.0f, 20000.0f - 16000.0f * depth);
                chewLPF_[0].setCutoff(cutoff, sr_);
                chewLPF_[1].setCutoff(cutoff, sr_);
                chewSamplesUntilChange_ = (int)(sr_ * (0.08f + 0.25f * rand01) * varJitter);
            } else {
                chewTargetMix_ = 0.0f;
                chewTargetPow_ = 1.0f;
                chewLPF_[0].setCutoff(20000.0f, sr_);
                chewLPF_[1].setCutoff(20000.0f, sr_);
                chewSamplesUntilChange_ = (int)(sr_ * (0.20f + 1.4f * (1.0f - 0.85f * freq) * rand01) * varJitter);
            }
        }
    }

    inline float stepWowFlutterLFO8(int ch, float baseWowFreq, float wowAmpSamples,
                                    float wowVarParam, float wowDriftParam,
                                    float flutDelta8, float flutAmpSamples,
                                    float wowDepthParam, float flutDepthParam,
                                    float& outLfoNorm) noexcept {
        const float fs = (float)sr_;
        auto& wf = wowFlut_[ch];

        const float wowFreq = baseWowFreq * (1.0f + 0.25f * wowDriftParam * wf.ouState);
        const float wowDelta8 = 8.0f * (2.0f * 3.14159265358979323846f * std::max(0.0f, wowFreq) / fs);
        wf.wowPhase += wowDelta8;
        if (wf.wowPhase >= 6.283185307179586f)
            wf.wowPhase -= 6.283185307179586f;

        wf.ouState += (-0.016f * wf.ouState + 0.014f * (0.2f + 0.8f * wowVarParam) * rng_.nextBipolar());
        wf.ouState = clamp(wf.ouState, -0.7f, 0.7f);

        const float wowWave = fastCos(wf.wowPhase + (ch * 0.15f)) + wowVarParam * wf.ouState;
        const float wowLFO = wowAmpSamples * (1.0f + wowWave);

        wf.flutPhase1 += flutDelta8;
        wf.flutPhase2 += 2.0f * flutDelta8;
        wf.flutPhase3 += 3.0f * flutDelta8;
        if (wf.flutPhase1 >= 6.283185307179586f) wf.flutPhase1 -= 6.283185307179586f;
        if (wf.flutPhase2 >= 6.283185307179586f) wf.flutPhase2 -= 6.283185307179586f;
        if (wf.flutPhase3 >= 6.283185307179586f) wf.flutPhase3 -= 6.283185307179586f;

        constexpr float phaseOff2 = 13.0f * 3.14159265358979323846f / 4.0f;
        constexpr float phaseOff3 = -3.14159265358979323846f / 10.0f;
        const float flutWave = -0.230f * fastCos(wf.flutPhase1)
                             - 0.080f * fastCos(wf.flutPhase2 + phaseOff2)
                             - 0.099f * fastCos(wf.flutPhase3 + phaseOff3)
                             + 0.350f;
        const float flutSamples = flutAmpSamples * flutWave;

        if (ch == 0) {
            const float combined = 0.5f + 0.30f * wowDepthParam * wowWave + 0.35f * flutDepthParam * (flutWave - 0.35f) * 2.5f;
            outLfoNorm = clamp(combined, 0.0f, 1.0f);
        }

        return 2.0f + wowLFO + flutSamples;
    }

    double sr_ = 44100.0;
    HysteresisChannel hyst_[2];
    DCBlocker hystDc_[2];
    float hystMakeup_ = 1.0f;
    float prevDrive_ = -1.0f, prevSat_ = -1.0f, prevBias_ = -1.0f;
    float prevOutDb_ = -999.0f, prevInDb_ = -999.0f;

    Biquad inLowCut_[2];
    Biquad inHighCut_[2];
    float prevLowCut_ = -1.0f, prevHighCut_ = -1.0f;

    ShelfFilter toneIn_[2];
    ShelfFilter toneOut_[2];
    float prevBass_ = -99.0f, prevTreble_ = -99.0f, prevTFreq_ = -1.0f;

    TapeCompressor comp_[2];
    float prevCompAtk_ = -1.0f, prevCompRel_ = -1.0f;

    LossAndBumpChannel loss_[2];
    float azDelaySamp_[2]{};
    float prevSpeed_ = -1.0f, prevSpacing_ = -1.0f, prevThick_ = -1.0f, prevGap_ = -1.0f;
    bool updatingSpeedPreset_ = false;

    WowFlutterChannel wowFlut_[2];
    OnePoleLP chewLPF_[2];
    OnePoleLP degLPF_[2];
    float degEnv_ = 0.0f;
    float degGainLin_ = 1.0f;
    PinkNoise noise_;
    Xorshift32 rng_;

    int chewCounter_ = 0;
    int chewSamplesUntilChange_ = 8000;
    bool isCrinkled_ = false;
    float chewTargetMix_ = 0.0f;
    float chewTargetPow_ = 1.0f;
    float chewMixSmooth_ = 0.0f;
    float chewPowSmooth_ = 1.0f;

    float meterInPeak_ = 0.0f;
    float meterOutPeak_ = 0.0f;
    float lastSentInDb_ = -60.0f;
    float lastSentOutDb_ = -60.0f;
    float lastSentLfo_ = 0.5f;
    int meterCounter_ = 0;
    uint64_t totalFrames_ = 0;

    Smoother outGainSmooth_, inGainSmooth_, driveSmooth_, satSmooth_, biasSmooth_, mixSmooth_;
    uint32_t controlCounter_ = 0;
};

MPC_PLUGIN_ENTRY(ChowTapeMPC)
