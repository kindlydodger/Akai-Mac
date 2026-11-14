#include <M5Unified.h>
#include <USB-MIDI.h>       
#include <USBHIDKeyboard.h> 
#include "Unit_8Encoder.h"  
#include <Preferences.h>    
#include <WiFi.h>
#include <WiFiUdp.h>
#include <WebServer.h>
#include <WebSocketsServer.h>
#include <HTTPClient.h>
#include <Update.h>         
#include <SD.h>
#include <SPI.h>
#include <math.h>

USBMIDI MIDI;
USBHIDKeyboard Keyboard; 
Unit_8Encoder encoder;
Preferences preferences;    
WebServer server(80);
WebSocketsServer webSocket(81);

// SoftAP Topology: Cardputer Gateway = 192.168.4.1, Hakai OS Client = 192.168.4.2
String wifiSsid = "MPCUTER"; 
String wifiPassword = "mpc60password";
String targetMpcIp = "192.168.4.2"; 

WiFiUDP udp;
const int localPort = 8888;
char udpPacketBuffer[255];

WiFiUDP logUdp;
const int logPort = 8889;
char logPacketBuffer[255];
String logBuffer[6] = {"", "", "", "", "", ""};

bool hakaiConnected = false; 
unsigned long lastHakaiHeartbeat = 0;
const unsigned long hakaiTimeout = 3000; 

int currentRssi = -55;
float currentBPM = 120.0;
int masterAudioLeft = 78;   
int masterAudioRight = 74;  
int currentBank = 0; 
int currentChannel = 1; 
int activeEncoderIdx = 0;
int menuSelection = 0;
bool gyroEnabled = true;

enum ControllerMode { MODE_QLINK, MODE_MENU, MODE_SD_DEPLOY, MODE_LOG_VIEWER };
ControllerMode currentMode = MODE_QLINK;

// MPC60 Monochrome Green LCD Palette (RGB565)
#define MPC60_BG      0x0881  // #0A1408 Dark Olive Green
#define MPC60_GREEN   0x27E8  // #24FF42 Bright Phosphor Green
#define MPC60_DIM     0x12A3  // #12551C Dim Grid Green

const uint8_t qlinkCC[4][8] = {
  {70, 71, 72, 73, 74, 75, 76, 77},
  {78, 79, 80, 81, 82, 83, 84, 85}, 
  {86, 87, 88, 89, 90, 91, 92, 93},
  {10, 12, 13, 75, 76, 91, 92, 93}  
};

const char* qlinkLabels[4][8] = {
  {"QLK1:CUTOFF", "QLK2:RESO",   "QLK3:ATTACK", "QLK4:DECAY",  "QLK5:SUST",   "QLK6:REL",    "QLK7:LFO R",  "QLK8:LFO D"},
  {"QLK1:REVERB", "QLK2:DELAY",  "QLK3:CHORUS", "QLK4:DIST",   "QLK5:GAIN",   "QLK6:PAN",    "QLK7:EQ LOW", "QLK8:EQ HI"},
  {"QLK1:TUNE",   "QLK2:FINE",   "QLK3:LEVEL",  "QLK4:PAN",    "QLK5:CUTOFF", "QLK6:RESO",   "QLK7:DECAY",  "QLK8:ATTACK"},
  {"QLK1:PAN",    "QLK2:EFFECT", "QLK3:SEND A", "QLK4:SEND B", "QLK5:LFO",    "QLK6:FILTER", "QLK7:PITCH",  "QLK8:MOD"}
};

uint8_t encoderValues[4][8] = {
  {96, 42, 18, 110, 64, 85, 30, 127},
  {64, 64, 64,  64, 64, 64, 64,  64},
  { 0,  0, 100, 64, 80, 20, 50,  10},
  {64,  0,  40, 25,  0, 90, 64,   0}
};

String sdFileList[15];
int sdFileCount = 0;
int selectedSdIndex = 0;
bool isSdLoaded = false;

void drawMainUI();
void drawMenuUI();
void drawSdDeployUI();
void drawLogViewerUI();

// Zero-Allocation In-Place CSV Telemetry Parser (<0.2ms)
void parseRawUdpPacket() {
  int packetSize = udp.parsePacket();
  if (packetSize > 0) {
    int len = udp.read(udpPacketBuffer, 254);
    if (len <= 0) return;
    udpPacketBuffer[len] = 0;
    char* token = strtok(udpPacketBuffer, ",");
    while (token != NULL) {
      if (strncmp(token, "L:", 2) == 0) masterAudioLeft = atoi(token + 2);
      else if (strncmp(token, "R:", 2) == 0) masterAudioRight = atoi(token + 2);
      else if (strncmp(token, "BPM:", 4) == 0) currentBPM = atof(token + 4);
      else if (strncmp(token, "H:", 2) == 0) {
        hakaiConnected = (atoi(token + 2) == 1);
        if (hakaiConnected) lastHakaiHeartbeat = millis();
      }
      token = strtok(NULL, ",");
    }
  }
  if (millis() - lastHakaiHeartbeat > hakaiTimeout) hakaiConnected = false;
}

void pollLogs() {
  int packetSize = logUdp.parsePacket();
  if (packetSize) {
    int len = logUdp.read(logPacketBuffer, 254);
    logPacketBuffer[len] = 0;
    for (int i = 0; i < 5; i++) logBuffer[i] = logBuffer[i + 1];
    logBuffer[5] = String(logPacketBuffer);
    if (currentMode == MODE_LOG_VIEWER) drawLogViewerUI();
  }
}

void loadSdPythonFiles() {
  sdFileCount = 0;
  File root = SD.open("/");
  if (!root) return;
  File file = root.openNextFile();
  while (file && sdFileCount < 15) {
    if (!file.isDirectory()) {
      String fileName = String(file.name());
      if (fileName.endsWith(".py") || fileName.endsWith(".PY")) {
        sdFileList[sdFileCount] = fileName;
        sdFileCount++;
      }
    }
    file = root.openNextFile();
  }
  isSdLoaded = true;
}

String escapeJsonString(const String& src) {
  String out;
  out.reserve(src.length() + 16);
  for (size_t i = 0; i < src.length(); i++) {
    char c = src[i];
    if (c == '\\') out += "\\\\";
    else if (c == '\"') out += "\\\"";
    else if (c == '\n') out += "\\n";
    else if (c == '\r') out += "\\r";
    else if (c == '\t') out += "\\t";
    else if ((uint8_t)c >= 0x20) out += c;
  }
  return out;
}

void deploySelectedScriptFromSd(String filename) {
  String sdPath = filename.startsWith("/") ? filename : ("/" + filename);
  File file = SD.open(sdPath, FILE_READ);
  if (!file) return;
  String fileContent = "";
  while (file.available()) fileContent += (char)file.read();
  file.close();

  fileContent = escapeJsonString(fileContent);

  HTTPClient http;
  http.setTimeout(800);
  http.begin("http://" + targetMpcIp + ":5000/deploy");
  http.addHeader("Content-Type", "application/json");
  String payload = "{\"filename\":\"" + filename + "\",\"content\":\"" + fileContent + "\",\"auto_run\":true}";
  http.POST(payload);
  http.end();
}

void triggerCloudBackup(String targetMode) {
  String sdFilesJson = "{";
  File root = SD.open("/");
  if (root) {
    File file = root.openNextFile();
    bool first = true;
    while (file) {
      if (!file.isDirectory()) {
        String fname = String(file.name());
        if (fname.endsWith(".py") || fname.endsWith(".json")) {
          String content = "";
          while (file.available()) content += (char)file.read();
          content.replace("\\", "\\\\");
          content.replace("\"", "\\\"");
          content.replace("\r", "");
          content.replace("\n", "\\n");
          if (!first) sdFilesJson += ",";
          sdFilesJson += "\"" + fname + "\":\"" + content + "\"";
          first = false;
        }
      }
      file.close();
      file = root.openNextFile();
    }
  }
  sdFilesJson += "}";

  HTTPClient http;
  http.setTimeout(1500);
  http.begin("http://" + targetMpcIp + ":5000/cloud-backup");
  http.addHeader("Content-Type", "application/json");
  String payload = "{\"target\":\"" + targetMode + "\",\"sd_files\":" + sdFilesJson + "}";
  int httpCode = http.POST(payload);
  http.end();

  if (httpCode > 0) {
    currentMode = MODE_LOG_VIEWER;
    drawLogViewerUI();
  }
}

void drawPixelArc(int cx, int cy, int r, int startDeg, int endDeg, uint16_t color) {
  for (int deg = startDeg; deg <= endDeg; deg += 3) {
    float rad = deg * 0.0174533f;
    int px = round(cx + r * cos(rad));
    int py = round(cy + r * sin(rad));
    M5.Lcd.drawPixel(px, py, color);
  }
}

void drawSoftKeyBar(const char* keys[7], int activeIdx) {
  M5.Lcd.drawFastHLine(0, 114, 240, MPC60_GREEN);
  for (int k = 0; k < 7; k++) {
    int kx = 2 + (k * 34);
    int tx = kx + (31 - strlen(keys[k]) * 6) / 2;
    if (k == activeIdx) {
      M5.Lcd.fillRect(kx, 117, 31, 15, MPC60_GREEN);
      M5.Lcd.setTextColor(MPC60_BG, MPC60_GREEN);
    } else {
      M5.Lcd.fillRect(kx, 117, 31, 15, MPC60_BG);
      M5.Lcd.drawRect(kx, 117, 31, 15, MPC60_GREEN);
      M5.Lcd.setTextColor(MPC60_GREEN, MPC60_BG);
    }
    M5.Lcd.setCursor(tx, 121);
    M5.Lcd.print(keys[k]);
  }
}

void drawEncoderCell(int idx) {
  int col = idx % 4;
  int row = idx / 4;
  int x = 2 + col * 59;
  int y = 16 + row * 49;
  int w = 58, h = 48;

  bool active = (idx == activeEncoderIdx);
  uint8_t val = encoderValues[currentBank][idx];
  uint8_t cc  = qlinkCC[currentBank][idx];
  const char* rawLabel = qlinkLabels[currentBank][idx] + 5;

  M5.Lcd.fillRect(x, y, w, h, MPC60_BG);
  M5.Lcd.drawRect(x, y, w, h, active ? MPC60_GREEN : MPC60_DIM);

  if (active) {
    M5.Lcd.fillRect(x + 1, y + 1, w - 2, 11, MPC60_GREEN);
    M5.Lcd.setTextColor(MPC60_BG, MPC60_GREEN);
    M5.Lcd.setCursor(x + 3, y + 3);
    M5.Lcd.printf("*%d:%-6.6s", idx + 1, rawLabel);
  } else {
    M5.Lcd.setTextColor(MPC60_GREEN, MPC60_BG);
    M5.Lcd.setCursor(x + 3, y + 3);
    M5.Lcd.printf(" %d:%-6.6s", idx + 1, rawLabel);
    M5.Lcd.drawFastHLine(x + 1, y + 12, w - 2, MPC60_DIM);
  }

  int cx = x + 15, cy = y + 29;
  int rOuter = 10, rInner = 7;
  int valEndDeg = 135 + (val * 270) / 127;

  drawPixelArc(cx, cy, rOuter, 135, 405, MPC60_DIM);
  drawPixelArc(cx, cy, rOuter, 135, valEndDeg, MPC60_GREEN);
  drawPixelArc(cx, cy, rOuter - 1, 135, valEndDeg, MPC60_GREEN);
  drawPixelArc(cx, cy, rInner, 0, 360, MPC60_GREEN);

  float rad = valEndDeg * 0.0174533f;
  int px = round(cx + (rInner - 1) * cos(rad));
  int py = round(cy + (rInner - 1) * sin(rad));
  M5.Lcd.drawLine(cx, cy, px, py, MPC60_GREEN);

  int barW = (val * 24) / 127;
  M5.Lcd.drawRect(x + 3, y + 42, 25, 3, MPC60_DIM);
  if (barW > 0) M5.Lcd.fillRect(x + 3, y + 42, barW, 3, MPC60_GREEN);

  M5.Lcd.setTextColor(MPC60_GREEN, MPC60_BG);
  M5.Lcd.setCursor(x + 29, y + 16);
  M5.Lcd.printf("CC:%02d", cc);

  if (active) {
    M5.Lcd.fillRect(x + 29, y + 28, 27, 12, MPC60_GREEN);
    M5.Lcd.setTextColor(MPC60_BG, MPC60_GREEN);
  } else {
    M5.Lcd.drawRect(x + 29, y + 28, 27, 12, MPC60_GREEN);
    M5.Lcd.setTextColor(MPC60_GREEN, MPC60_BG);
  }
  M5.Lcd.setCursor(x + 31, y + 30);
  M5.Lcd.printf("V:%03d", val);
}

void drawMainUI() {
  M5.Lcd.fillScreen(MPC60_BG);
  M5.Lcd.setTextSize(1);

  M5.Lcd.fillRect(2, 2, 104, 11, MPC60_GREEN);
  M5.Lcd.setTextColor(MPC60_BG, MPC60_GREEN);
  M5.Lcd.setCursor(5, 3);
  M5.Lcd.print("8-ENCODER Q-LINK");

  M5.Lcd.setTextColor(MPC60_GREEN, MPC60_BG);
  M5.Lcd.setCursor(110, 3);
  M5.Lcd.printf("CH:%02d BNK:%c [H:%s]", currentChannel, 'A' + currentBank, hakaiConnected ? "OK" : "--");
  M5.Lcd.drawFastHLine(0, 14, 240, MPC60_GREEN);

  for (int i = 0; i < 8; i++) drawEncoderCell(i);

  const char* sk[7] = {"QLNK", "MIXR", "GYRO", "DPLY", "LOGS", "CLUD", "MENU"};
  drawSoftKeyBar(sk, 0);
}

void drawMenuUI() {
  M5.Lcd.fillScreen(MPC60_BG);
  M5.Lcd.setTextSize(1);

  M5.Lcd.fillRect(2, 2, 114, 11, MPC60_GREEN);
  M5.Lcd.setTextColor(MPC60_BG, MPC60_GREEN);
  M5.Lcd.setCursor(5, 3);
  M5.Lcd.print("MPCUTER SYSTEM HUB");

  M5.Lcd.setTextColor(MPC60_GREEN, MPC60_BG);
  M5.Lcd.setCursor(120, 3);
  M5.Lcd.printf("BPM:%.1f [H:%s]", currentBPM, hakaiConnected ? "OK" : "--");
  M5.Lcd.drawFastHLine(0, 14, 240, MPC60_GREEN);

  const char* options[6] = {
    "1 SD DEPLOYER",
    "2 LIVE LOGS",
    "3 CLOUD BACKUP",
    "4 GYRO MOTION",
    "5 LAZY NVS SAVE",
    "6 WEB OTA & IP"
  };

  for (int i = 0; i < 6; i++) {
    int y = 18 + (i * 15);
    if (i == menuSelection) {
      M5.Lcd.fillRect(2, y, 116, 13, MPC60_GREEN);
      M5.Lcd.drawRect(5, y + 3, 6, 6, MPC60_BG);
      M5.Lcd.setTextColor(MPC60_BG, MPC60_GREEN);
      M5.Lcd.setCursor(14, y + 2);
      M5.Lcd.printf("*%s", options[i]);
    } else {
      M5.Lcd.drawRect(5, y + 3, 6, 6, MPC60_DIM);
      M5.Lcd.setTextColor(MPC60_GREEN, MPC60_BG);
      M5.Lcd.setCursor(14, y + 2);
      M5.Lcd.printf(" %s", options[i]);
    }
  }

  M5.Lcd.drawFastVLine(121, 15, 98, MPC60_GREEN);

  M5.Lcd.fillRect(125, 18, 111, 11, MPC60_GREEN);
  M5.Lcd.setTextColor(MPC60_BG, MPC60_GREEN);
  M5.Lcd.setCursor(129, 20);
  M5.Lcd.print("STATUS & VU METER");

  M5.Lcd.setTextColor(MPC60_GREEN, MPC60_BG);
  M5.Lcd.setCursor(125, 34); M5.Lcd.printf("MPC IP:%s", targetMpcIp.c_str());
  M5.Lcd.setCursor(125, 46); M5.Lcd.print("WIFI  :SOFT-AP OK");
  M5.Lcd.setCursor(125, 58); M5.Lcd.print("FLASH :LAZY MODE");
  M5.Lcd.setCursor(125, 70); M5.Lcd.printf("SD CRD:%d .PY FILES", sdFileCount);

  M5.Lcd.setCursor(125, 86); M5.Lcd.print("L");
  M5.Lcd.drawRect(133, 88, 77, 5, MPC60_GREEN);
  M5.Lcd.fillRect(134, 89, (constrain(masterAudioLeft, 0, 100) * 75) / 100, 3, MPC60_GREEN);
  M5.Lcd.setCursor(213, 85); M5.Lcd.printf("%2d%%", masterAudioLeft);

  M5.Lcd.setCursor(125, 98); M5.Lcd.print("R");
  M5.Lcd.drawRect(133, 100, 77, 5, MPC60_GREEN);
  M5.Lcd.fillRect(134, 101, (constrain(masterAudioRight, 0, 100) * 75) / 100, 3, MPC60_GREEN);
  M5.Lcd.setCursor(213, 97); M5.Lcd.printf("%2d%%", masterAudioRight);

  const char* sk[7] = {"SLCT", "UP", "DOWN", "DPLY", "LOGS", "SAVE", "BACK"};
  drawSoftKeyBar(sk, 0);
}

void drawSdDeployUI() {
  M5.Lcd.fillScreen(MPC60_BG);
  M5.Lcd.setTextSize(1);

  M5.Lcd.fillRect(2, 2, 118, 11, MPC60_GREEN);
  M5.Lcd.setTextColor(MPC60_BG, MPC60_GREEN);
  M5.Lcd.setCursor(5, 3);
  M5.Lcd.print("SD SCRIPT DEPLOYER");
  M5.Lcd.setTextColor(MPC60_GREEN, MPC60_BG);
  M5.Lcd.setCursor(124, 3);
  M5.Lcd.print("TARGET: :5000/DPLY");
  M5.Lcd.drawFastHLine(0, 14, 240, MPC60_GREEN);

  if (!isSdLoaded || sdFileCount == 0) {
    M5.Lcd.setCursor(15, 50);
    M5.Lcd.print("No .py files found on SD!");
  } else {
    for (int i = 0; i < min(sdFileCount, 5); i++) {
      int y = 19 + (i * 17);
      if (i == selectedSdIndex) {
        M5.Lcd.fillRect(2, y, 236, 14, MPC60_GREEN);
        M5.Lcd.setTextColor(MPC60_BG, MPC60_GREEN);
        M5.Lcd.setCursor(6, y + 3);
        M5.Lcd.printf("* %-24s [RUN]", sdFileList[i].c_str());
      } else {
        M5.Lcd.drawRect(2, y, 236, 14, MPC60_DIM);
        M5.Lcd.setTextColor(MPC60_GREEN, MPC60_BG);
        M5.Lcd.setCursor(6, y + 3);
        M5.Lcd.printf("  %-24s [PY]", sdFileList[i].c_str());
      }
    }
  }

  const char* sk[7] = {"RUN", "UP", "DOWN", "CLUD", "LOGS", "REFR", "BACK"};
  drawSoftKeyBar(sk, 0);
}

void drawLogViewerUI() {
  M5.Lcd.fillScreen(MPC60_BG);
  M5.Lcd.setTextSize(1);

  M5.Lcd.fillRect(2, 2, 108, 11, MPC60_GREEN);
  M5.Lcd.setTextColor(MPC60_BG, MPC60_GREEN);
  M5.Lcd.setCursor(5, 3);
  M5.Lcd.print("HAKAI LIVE LOGS");
  M5.Lcd.setTextColor(MPC60_GREEN, MPC60_BG);
  M5.Lcd.setCursor(114, 3);
  M5.Lcd.print("UDP:8889 [STREAM]");
  M5.Lcd.drawFastHLine(0, 14, 240, MPC60_GREEN);

  for (int i = 0; i < 6; i++) {
    int y = 19 + (i * 15);
    if (i == 5 && logBuffer[i].length() > 0) {
      M5.Lcd.fillRect(2, y, 236, 13, MPC60_GREEN);
      M5.Lcd.setTextColor(MPC60_BG, MPC60_GREEN);
    } else {
      M5.Lcd.setTextColor(MPC60_GREEN, MPC60_BG);
    }
    M5.Lcd.setCursor(5, y + 2);
    M5.Lcd.printf("> %s", logBuffer[i].c_str());
  }

  const char* sk[7] = {"CLR", "PAUS", "DPLY", "CLUD", "PING", "QLNK", "BACK"};
  drawSoftKeyBar(sk, 5);
}

void setup() {
  auto cfg = M5.config();
  M5.begin(cfg);
  M5.Lcd.setRotation(1);
  Keyboard.begin(); 

  preferences.begin("midi-config", true);
  targetMpcIp = preferences.getString("mpc_ip", "192.168.4.2");
  preferences.end();

  WiFi.softAP(wifiSsid.c_str(), wifiPassword.c_str());
  server.begin();
  webSocket.begin();
  
  encoder.begin(&Wire, 0x41, 2, 1);
  MIDI.begin();
  udp.begin(localPort);
  logUdp.begin(logPort);

  if (SD.begin()) loadSdPythonFiles();
  drawMainUI();
}

void loop() {
  M5.update();
  server.handleClient(); 
  webSocket.loop(); 
  parseRawUdpPacket();
  pollLogs();

  for (int i = 0; i < 8; i++) {
    int inc = encoder.getIncrementValue(i);
    if (inc != 0) {
      int prevIdx = activeEncoderIdx;
      activeEncoderIdx = i;
      int newVal = constrain((int)encoderValues[currentBank][i] + inc, 0, 127);
      encoderValues[currentBank][i] = (uint8_t)newVal;
      MIDI.sendControlChange(qlinkCC[currentBank][i], newVal, currentChannel);
      if (currentMode == MODE_QLINK) {
        if (prevIdx != activeEncoderIdx) drawEncoderCell(prevIdx);
        drawEncoderCell(i);
      }
    }
  }

  if (M5.Keyboard.isChange() && M5.Keyboard.isPressed()) {
    for (auto& key : M5.Keyboard.keys) {
      if (key == 'm' || key == 'M') { currentMode = MODE_MENU; menuSelection = 0; drawMenuUI(); }
      else if (key == 'l' || key == 'L') { currentMode = MODE_LOG_VIEWER; drawLogViewerUI(); }
      else if (key == 'q' || key == 'Q' || key == 27) { currentMode = MODE_QLINK; drawMainUI(); }
      else if (currentMode == MODE_MENU) {
        if (key == 'w' || key == 'W') { menuSelection = (menuSelection + 5) % 6; drawMenuUI(); }
        else if (key == 's' || key == 'S') { menuSelection = (menuSelection + 1) % 6; drawMenuUI(); }
        else if (key == '\n' || key == '\r') {
          if (menuSelection == 0) { currentMode = MODE_SD_DEPLOY; loadSdPythonFiles(); drawSdDeployUI(); }
          else if (menuSelection == 1) { currentMode = MODE_LOG_VIEWER; drawLogViewerUI(); }
          else if (menuSelection == 2) { triggerCloudBackup("all"); }
          else if (menuSelection == 4) {
            preferences.begin("midi-config", false);
            preferences.putString("mpc_ip", targetMpcIp);
            preferences.end();
          }
        }
      }
      else if (currentMode == MODE_SD_DEPLOY) {
        if (key == 'w' || key == 'W') { selectedSdIndex = (selectedSdIndex + sdFileCount - 1) % max(1, sdFileCount); drawSdDeployUI(); }
        else if (key == 's' || key == 'S') { selectedSdIndex = (selectedSdIndex + 1) % max(1, sdFileCount); drawSdDeployUI(); }
        else if (key == '\n' || key == '\r') {
          if (sdFileCount > 0) { deploySelectedScriptFromSd(sdFileList[selectedSdIndex]); currentMode = MODE_LOG_VIEWER; drawLogViewerUI(); }
        }
      }
    }
  }
}
