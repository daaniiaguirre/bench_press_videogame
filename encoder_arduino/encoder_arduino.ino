// AM4096 12-bit magnetic encoder — SSI read on Arduino UNO R4
// Wiring:  RMK4 Vext → 3.3V,  GND → GND,  Clock → D2,  Data → D3

const int PIN_CLK  = 2;   // SSI clock output (Arduino drives this)
const int PIN_DATA = 3;   // SSI data input  (encoder drives this)

const int SSI_BITS = 12;              // 12-bit resolution = 4096 counts/rev

// Monoflop timing from datasheet:
// tCL min = 0.25 µs → half-period = 1 µs gives ~500 kHz (well within spec)
// After LSB, wait > tmMax = 25 µs before next read
const int HALF_CLK_US = 1;           // half clock period in µs
const int MONOFLOP_WAIT_US = 40;     // wait after burst (> 25 µs tmMax)

void setup() {
  Serial.begin(115200);
  while (!Serial);                    // wait for serial on UNO R4
  pinMode(PIN_CLK,  OUTPUT);
  pinMode(PIN_DATA, INPUT);
  digitalWrite(PIN_CLK, HIGH);       // clock idles HIGH per SSI spec
  delay(50);                          // let encoder power up and settle
  Serial.println("AM4096 SSI encoder reader ready");
  Serial.println("Raw counts (0-4095) | Angle (degrees)");
}

// Read one 12-bit SSI frame from the AM4096.
// Protocol:
//   1. First HIGH→LOW transition latches current position into shift register
//      and triggers the monoflop.
//   2. Each subsequent LOW→HIGH transition clocks out the next bit (MSB first).
//   3. After the LSB the Data line goes LOW.
//   4. Wait > tmMax before the next read.
uint16_t readSSI() {
  uint16_t position = 0;

  // ── Pulse 1: HIGH→LOW latches position, LOW→HIGH clocks MSB out ──────────
  digitalWrite(PIN_CLK, LOW);
  delayMicroseconds(HALF_CLK_US);
  digitalWrite(PIN_CLK, HIGH);
  delayMicroseconds(HALF_CLK_US);

  // MSB is now valid on Data — read it and the remaining 11 bits
  for (int i = SSI_BITS - 1; i >= 0; i--) {
    // Data is valid on the rising edge we just produced (or will produce)
    position |= ((uint16_t)digitalRead(PIN_DATA) << i);

    if (i > 0) {                      // don't generate an extra clock after LSB
      digitalWrite(PIN_CLK, LOW);
      delayMicroseconds(HALF_CLK_US);
      digitalWrite(PIN_CLK, HIGH);
      delayMicroseconds(HALF_CLK_US);
    }
  }

  // Leave clock HIGH (idle state) and wait for monoflop to expire
  delayMicroseconds(MONOFLOP_WAIT_US);

  return position & 0x0FFF;           // mask to 12 bits just in case
}

void loop() {
  uint16_t raw   = readSSI();
  float    angle = (float)raw * 360.0f / 4096.0f;

  Serial.print("Raw: ");
  Serial.print(raw);
  Serial.print(" / 4095   |   Angle: ");
  Serial.print(angle, 2);
  Serial.println(" °");

  delay(100);                         // 10 readings per second
}