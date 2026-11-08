/*
 * fan_zone_control.ino  —  proxmox-fancontrol firmware v1
 * 3-Zone PWM fan controller for Arduino Nano (ATmega328P)
 *
 * Zones (PWM out):
 *   Zone 1 → Pin  9 (OC1A, Timer1) — Front fans: drive stack cooling
 *   Zone 2 → Pin 10 (OC1B, Timer1) — Top fans:   CPU + airflow
 *   Zone 3 → Pin 11 (OC2A, Timer2) — Rear fan:   CPU + airflow
 *
 * Tach (optional, RPM in): connect ONE fan's tach wire per zone
 *   Zone 1 → D2 (INT0)   Zone 2 → D3 (INT1)   Zone 3 → D4 (PCINT20)
 *   Internal pull-ups are enabled. Never join tach wires of several fans.
 *   Unconnected tach pins simply report 0.
 *
 * PWM frequency:
 *   Pins 9 & 10 (Timer1): 25 kHz   — proper 4-pin PWM fan spec
 *   Pin  11    (Timer2):  ~31.4 kHz — close enough for most fans
 *
 * Serial protocol (9600 baud, newline terminated):
 *   "Z1:128"  → set zone 1 to PWM 128 (0–255)       reply "OK Z1:128"
 *   "A:128"   → set all zones                        reply "OK ALL:128"
 *   "?"       → query PWM                            reply "Z1:128 Z2:200 Z3:50"
 *   "V"       → firmware version                     reply "VER 1"
 *   "R"       → fan RPM per zone                     reply "R1:1200 R2:900 R3:0"
 *   "W:30"    → watchdog: if no command for 30 s, set all zones to
 *               FAILSAFE_PWM until the host talks again. "W:0" disables.
 *               Off at boot; the daemon enables it when it connects.
 */

// ── Pin assignments ──────────────────────────────────────────────
const uint8_t PIN_ZONE1 = 9;   // Front fans  (Timer1A)
const uint8_t PIN_ZONE2 = 10;  // Top fans    (Timer1B)
const uint8_t PIN_ZONE3 = 11;  // Rear fan    (Timer2A)

const uint8_t PIN_TACH1 = 2;   // INT0
const uint8_t PIN_TACH2 = 3;   // INT1
const uint8_t PIN_TACH3 = 4;   // PCINT20 (port D)

// ── Tunables ─────────────────────────────────────────────────────
const uint8_t  PWM_DEFAULT      = 50;    // on power-up, until host sets values
const uint8_t  FAILSAFE_PWM     = 200;   // watchdog fired: host went silent
const uint8_t  TACH_PULSES_REV  = 2;     // standard PC fans: 2 pulses / revolution
const uint16_t TACH_WINDOW_MS   = 2000;  // RPM measurement window
const uint16_t TACH_DEBOUNCE_US = 1500;  // ignore edges closer than this (noise)

// ── State ─────────────────────────────────────────────────────────
uint8_t  zoneValues[4] = {0, PWM_DEFAULT, PWM_DEFAULT, PWM_DEFAULT}; // index 1–3
uint8_t  hostValues[4] = {0, PWM_DEFAULT, PWM_DEFAULT, PWM_DEFAULT}; // what host asked for

volatile uint16_t tachCount[4] = {0, 0, 0, 0};
volatile uint32_t tachLastUs[4] = {0, 0, 0, 0};
uint16_t rpm[4] = {0, 0, 0, 0};
uint32_t tachWindowStart = 0;

uint32_t watchdogMs     = 0;     // 0 = disabled
uint32_t lastCommandMs  = 0;
bool     failsafeActive = false;

// ── Timer1 25 kHz setup (pins 9 & 10) ────────────────────────────
void setupTimer1_25kHz() {
  // Fast PWM, ICR1 as TOP: 16e6 / (1 * (1 + 639)) = 25 000 Hz
  TCCR1A = _BV(COM1A1) | _BV(COM1B1) | _BV(WGM11);
  TCCR1B = _BV(WGM13)  | _BV(WGM12)  | _BV(CS10);  // prescaler=1
  ICR1   = 639;
  OCR1A  = 0;
  OCR1B  = 0;
}

void writeZone1(uint8_t val) { OCR1A = map(val, 0, 255, 0, ICR1); }
void writeZone2(uint8_t val) { OCR1B = map(val, 0, 255, 0, ICR1); }

// ── Timer2 ~31 kHz setup (pin 11) ────────────────────────────────
void setupTimer2_31kHz() {
  // Phase-correct PWM on OC2A, prescaler=1: 16e6 / 510 ≈ 31 372 Hz
  TCCR2A = _BV(COM2A1) | _BV(WGM20);
  TCCR2B = _BV(CS20);
  OCR2A  = 0;
}

void writeZone3(uint8_t val) { OCR2A = val; }

// ── Apply a value to a zone ───────────────────────────────────────
void applyZone(uint8_t zone, uint8_t val) {
  zoneValues[zone] = val;
  switch (zone) {
    case 1: writeZone1(val); break;
    case 2: writeZone2(val); break;
    case 3: writeZone3(val); break;
  }
}

void setZone(uint8_t zone, uint8_t val) {
  hostValues[zone] = val;
  if (!failsafeActive) applyZone(zone, val);
}

// ── Tach interrupts ───────────────────────────────────────────────
inline void tachEdge(uint8_t z) {
  uint32_t now = micros();
  if (now - tachLastUs[z] >= TACH_DEBOUNCE_US) {
    tachLastUs[z] = now;
    tachCount[z]++;
  }
}
void isrTach1() { tachEdge(1); }
void isrTach2() { tachEdge(2); }

volatile uint8_t lastPortD = 0;
ISR(PCINT2_vect) {               // D4 = PD4
  uint8_t pd = PIND;
  if ((lastPortD & _BV(PD4)) && !(pd & _BV(PD4))) tachEdge(3);  // falling edge
  lastPortD = pd;
}

void setupTach() {
  pinMode(PIN_TACH1, INPUT_PULLUP);
  pinMode(PIN_TACH2, INPUT_PULLUP);
  pinMode(PIN_TACH3, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(PIN_TACH1), isrTach1, FALLING);
  attachInterrupt(digitalPinToInterrupt(PIN_TACH2), isrTach2, FALLING);
  lastPortD = PIND;
  PCICR  |= _BV(PCIE2);          // enable port D pin-change interrupts
  PCMSK2 |= _BV(PCINT20);        // only D4
  tachWindowStart = millis();
}

void updateTach() {
  uint32_t now = millis();
  uint32_t elapsed = now - tachWindowStart;
  if (elapsed < TACH_WINDOW_MS) return;
  for (uint8_t z = 1; z <= 3; z++) {
    noInterrupts();
    uint16_t c = tachCount[z];
    tachCount[z] = 0;
    interrupts();
    rpm[z] = (uint32_t)c * 60000UL / (elapsed * TACH_PULSES_REV);
  }
  tachWindowStart = now;
}

// ── Watchdog (host silence → failsafe) ────────────────────────────
void updateWatchdog() {
  if (watchdogMs == 0 || failsafeActive) return;
  if (millis() - lastCommandMs > watchdogMs) {
    failsafeActive = true;
    for (uint8_t z = 1; z <= 3; z++) {
      applyZone(z, max(hostValues[z], FAILSAFE_PWM));
    }
  }
}

void leaveFailsafe() {
  if (!failsafeActive) return;
  failsafeActive = false;
  for (uint8_t z = 1; z <= 3; z++) applyZone(z, hostValues[z]);
}

// ── Serial command parser ─────────────────────────────────────────
char    inputBuffer[24];
uint8_t inputLen = 0;

uint8_t parsePwm(const char* s) {
  long v = atol(s);
  if (v < 0) v = 0;
  if (v > 255) v = 255;
  return (uint8_t)v;
}

void handleCommand(const char* cmd) {
  lastCommandMs = millis();
  leaveFailsafe();

  if (strcmp(cmd, "?") == 0) {
    Serial.print("Z1:");  Serial.print(zoneValues[1]);
    Serial.print(" Z2:"); Serial.print(zoneValues[2]);
    Serial.print(" Z3:"); Serial.println(zoneValues[3]);
    return;
  }

  if (strcmp(cmd, "V") == 0) {
    Serial.println("VER 1");
    return;
  }

  if (strcmp(cmd, "R") == 0) {
    Serial.print("R1:");  Serial.print(rpm[1]);
    Serial.print(" R2:"); Serial.print(rpm[2]);
    Serial.print(" R3:"); Serial.println(rpm[3]);
    return;
  }

  if (cmd[0] == 'W' && cmd[1] == ':') {
    long s = atol(cmd + 2);
    if (s < 0) s = 0;
    watchdogMs = (uint32_t)s * 1000UL;
    Serial.print("OK W:");
    Serial.println(s);
    return;
  }

  if (cmd[0] == 'A' && cmd[1] == ':') {
    uint8_t val = parsePwm(cmd + 2);
    setZone(1, val); setZone(2, val); setZone(3, val);
    Serial.print("OK ALL:");
    Serial.println(val);
    return;
  }

  if (cmd[0] == 'Z' && cmd[2] == ':' && strlen(cmd) >= 4) {
    uint8_t zone = cmd[1] - '0';
    if (zone >= 1 && zone <= 3) {
      uint8_t val = parsePwm(cmd + 3);
      setZone(zone, val);
      Serial.print("OK Z");
      Serial.print(zone);
      Serial.print(":");
      Serial.println(val);
      return;
    }
  }

  Serial.println("ERR unknown command");
}

// ── Arduino lifecycle ─────────────────────────────────────────────
void setup() {
  pinMode(PIN_ZONE1, OUTPUT);
  pinMode(PIN_ZONE2, OUTPUT);
  pinMode(PIN_ZONE3, OUTPUT);

  setupTimer1_25kHz();
  setupTimer2_31kHz();

  for (uint8_t z = 1; z <= 3; z++) setZone(z, PWM_DEFAULT);

  setupTach();

  Serial.begin(9600);
  Serial.println("READY");
  lastCommandMs = millis();
}

void loop() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      inputBuffer[inputLen] = '\0';
      // trim trailing spaces
      while (inputLen > 0 && inputBuffer[inputLen - 1] == ' ') inputBuffer[--inputLen] = '\0';
      if (inputLen > 0) handleCommand(inputBuffer);
      inputLen = 0;
    } else if (inputLen < sizeof(inputBuffer) - 1) {
      inputBuffer[inputLen++] = c;
    }
  }
  updateTach();
  updateWatchdog();
}
