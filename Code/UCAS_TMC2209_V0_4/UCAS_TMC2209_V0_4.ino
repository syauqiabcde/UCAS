/**
   UCAS / OTTO Liquid Handler — TMC2209 minimal motion firmware v0.5
   Target: Arduino DUE + 5x TMC2209

   Commands:
     <?>                       -> ready check
     <V>                       -> firmware/settings
     <D>                       -> Y1/Y2 TMC2209 diagnostics
     <M X100>                  -> relative X movement
     <M Y1600>                 -> relative Y movement
     <M X100 Y-50 Z20 P10>     -> simultaneous relative movement
*/

#include <TMCStepper.h>
#include <AccelStepper.h>
#include <string.h>
#include <stdlib.h>

// ============================================================
// STEP / DIR pins
// ============================================================

#define X_STEP_PIN  23
#define X_DIR_PIN   24

#define Y_STEP_PIN  27
#define Y_DIR_PIN   29

#define Z_STEP_PIN  32
#define Z_DIR_PIN   33

#define P_STEP_PIN  36
#define P_DIR_PIN   37


// ============================================================
// TMC2209 UART configuration
// ============================================================

#define R_SENSE 0.11f

// UART addresses on Serial1
#define DRV_ADDR_X   0b00
#define DRV_ADDR_Y1  0b01
#define DRV_ADDR_Y2  0b10
#define DRV_ADDR_Z   0b11

// P uses Serial2, so address 0 is fine
#define DRV_ADDR_P   0b00

#define DRIVER_SERIAL_XYZ Serial1
#define DRIVER_SERIAL_P   Serial2

#define DRIVER_BAUD 115200


// ============================================================
// Enable / disable UART configuration
// ============================================================

#define USE_UART_X   true
#define USE_UART_Y1  true
#define USE_UART_Y2  true

#define USE_UART_Z   false
#define USE_UART_P   false


// ============================================================
// TMC2209 driver objects
// ============================================================

TMC2209Stepper driverX(
  &DRIVER_SERIAL_XYZ,
  R_SENSE,
  DRV_ADDR_X
);

TMC2209Stepper driverY1(
  &DRIVER_SERIAL_XYZ,
  R_SENSE,
  DRV_ADDR_Y1
);

TMC2209Stepper driverY2(
  &DRIVER_SERIAL_XYZ,
  R_SENSE,
  DRV_ADDR_Y2
);

TMC2209Stepper driverZ(
  &DRIVER_SERIAL_XYZ,
  R_SENSE,
  DRV_ADDR_Z
);

TMC2209Stepper driverP(
  &DRIVER_SERIAL_P,
  R_SENSE,
  DRV_ADDR_P
);


// ============================================================
// AccelStepper motion objects
// ============================================================
//
// IMPORTANT:
// Y1 and Y2 share the SAME STEP/DIR signals.
// Therefore only ONE AccelStepper object is required for Y.
//
// Both TMC2209 drivers receive exactly the same STEP pulses.
//
// ============================================================

AccelStepper stepperX(
  AccelStepper::DRIVER,
  X_STEP_PIN,
  X_DIR_PIN
);

AccelStepper stepperY(
  AccelStepper::DRIVER,
  Y_STEP_PIN,
  Y_DIR_PIN
);

AccelStepper stepperZ(
  AccelStepper::DRIVER,
  Z_STEP_PIN,
  Z_DIR_PIN
);

AccelStepper stepperP(
  AccelStepper::DRIVER,
  P_STEP_PIN,
  P_DIR_PIN
);


// ============================================================
// Speed / acceleration settings
// ============================================================

// X = NEMA17
#define TEST_MAX_SPEED_X   600.0
#define TEST_ACCEL_X       500.0

// Y = dual larger motors
#define TEST_MAX_SPEED_Y   1500.0
#define TEST_ACCEL_Y       10000.0

// Z
#define TEST_MAX_SPEED_Z   6000.0
#define TEST_ACCEL_Z       3000.0

// Pipette
#define TEST_MAX_SPEED_P   6000.0
#define TEST_ACCEL_P       3000.0


// ============================================================
// Motor current
// ============================================================

#define X_CURRENT_MA   600

#define Y_CURRENT_MA   900

#define Z_CURRENT_MA   900

#define P_CURRENT_MA   500


// ============================================================
// Microstepping
// ============================================================

#define X_MICROSTEPS   16

#define Y_MICROSTEPS   16

#define Z_MICROSTEPS   16

#define P_MICROSTEPS    4


// ============================================================
// Chopper mode
// ============================================================

#define USE_SPREADCYCLE_X  false

#define USE_SPREADCYCLE_Y  true

#define USE_SPREADCYCLE_Z  true

#define USE_SPREADCYCLE_P  false


// ============================================================
// Serial command buffer
// ============================================================

const byte numChars = 80;

char receivedChars[numChars];
char tempChars[numChars];

boolean newData = false;


// ============================================================
// Configure one TMC2209
// ============================================================

static void configureDriver(
  TMC2209Stepper &drv,
  uint16_t rms_mA,
  uint16_t microsteps,
  bool spreadCycle
) {

  drv.begin();

  drv.toff(5);

  drv.blank_time(24);

  drv.rms_current(rms_mA);

  drv.microsteps(microsteps);

  drv.intpol(true);

  drv.pwm_autoscale(true);

  drv.TCOOLTHRS(0xFFFFF);

  drv.en_spreadCycle(spreadCycle);
}


// ============================================================
// Configure AccelStepper
// ============================================================

static void setupStepper(
  AccelStepper &s,
  float maxSpeed,
  float accel,
  bool dirInverted = false
) {

  s.setMaxSpeed(maxSpeed);

  s.setAcceleration(accel);

  // TMC2209 STEP pulse width
  s.setMinPulseWidth(20);

  s.setPinsInverted(
    dirInverted,
    false,
    false
  );
}


// ============================================================
// Print firmware settings
// ============================================================

static void printSettings() {

  Serial.println("<UCAS_TMC2209_v0.5>");

  Serial.println(
    "<pins X=23/24 Y=27/29 Z=32/33 P=36/37>"
  );

  Serial.print("<speed X=");
  Serial.print(TEST_MAX_SPEED_X);
  Serial.print("/");
  Serial.print(TEST_ACCEL_X);

  Serial.print(" Y=");
  Serial.print(TEST_MAX_SPEED_Y);
  Serial.print("/");
  Serial.print(TEST_ACCEL_Y);

  Serial.print(" Z=");
  Serial.print(TEST_MAX_SPEED_Z);
  Serial.print("/");
  Serial.print(TEST_ACCEL_Z);

  Serial.print(" P=");
  Serial.print(TEST_MAX_SPEED_P);
  Serial.print("/");
  Serial.print(TEST_ACCEL_P);

  Serial.println(">");


  Serial.print("<current X=");
  Serial.print(X_CURRENT_MA);

  Serial.print(" Y=");
  Serial.print(Y_CURRENT_MA);

  Serial.print(" Z=");
  Serial.print(Z_CURRENT_MA);

  Serial.print(" P=");
  Serial.print(P_CURRENT_MA);

  Serial.println(">");


  Serial.print("<microsteps X=");
  Serial.print(X_MICROSTEPS);

  Serial.print(" Y1=");
  Serial.print(Y_MICROSTEPS);

  Serial.print(" Y2=");
  Serial.print(Y_MICROSTEPS);

  Serial.print(" Z=");
  Serial.print(Z_MICROSTEPS);

  Serial.print(" P=");
  Serial.print(P_MICROSTEPS);

  Serial.println(">");


  Serial.println(
    "<uart X=on Y1=on Y2=on Z=off P=off>"
  );

  Serial.println(
    "<Y1_addr=1 Y2_addr=2>"
  );

  Serial.println(
    "<cmd <?> <V> <D> <M X100 Y-50 Z20 P10>>"
  );
}


// ============================================================
// Y1 / Y2 diagnostic
// ============================================================

static void printYDiagnostics() {

  Serial.println("<Y_DIAG_BEGIN>");


  // --------------------------------------------------------
  // Y1
  // --------------------------------------------------------

  uint8_t y1Connection = driverY1.test_connection();

  Serial.print("<Y1 connection=");
  Serial.print(y1Connection);

  Serial.print(" microsteps=");
  Serial.print(driverY1.microsteps());

  Serial.print(" current=");
  Serial.print(driverY1.rms_current());

  Serial.print("mA");

  Serial.print(" GSTAT=0x");
  Serial.print(driverY1.GSTAT(), HEX);

  Serial.print(" IOIN=0x");
  Serial.print(driverY1.IOIN(), HEX);

  Serial.println(">");


  // --------------------------------------------------------
  // Y2
  // --------------------------------------------------------

  uint8_t y2Connection = driverY2.test_connection();

  Serial.print("<Y2 connection=");
  Serial.print(y2Connection);

  Serial.print(" microsteps=");
  Serial.print(driverY2.microsteps());

  Serial.print(" current=");
  Serial.print(driverY2.rms_current());

  Serial.print("mA");

  Serial.print(" GSTAT=0x");
  Serial.print(driverY2.GSTAT(), HEX);

  Serial.print(" IOIN=0x");
  Serial.print(driverY2.IOIN(), HEX);

  Serial.println(">");


  // --------------------------------------------------------
  // Simple interpretation
  // --------------------------------------------------------

  if (y1Connection == 0 && y2Connection == 0) {

    Serial.println("<Y_UART_STATUS BOTH_OK>");

  }
  else if (y1Connection != 0 && y2Connection == 0) {

    Serial.println("<Y_UART_STATUS Y1_FAIL>");

  }
  else if (y1Connection == 0 && y2Connection != 0) {

    Serial.println("<Y_UART_STATUS Y2_FAIL>");

  }
  else {

    Serial.println("<Y_UART_STATUS BOTH_FAIL>");
  }


  Serial.println("<Y_DIAG_END>");
}


// ============================================================
// SETUP
// ============================================================

void setup() {

  // --------------------------------------------------------
  // USB serial
  // --------------------------------------------------------

  Serial.begin(115200);

  unsigned long t0 = millis();

  while (
    !Serial &&
    (millis() - t0 < 2000)
  ) {
    // wait maximum 2 seconds
  }


  // --------------------------------------------------------
  // TMC UART
  // --------------------------------------------------------

  DRIVER_SERIAL_XYZ.begin(DRIVER_BAUD);

  DRIVER_SERIAL_P.begin(DRIVER_BAUD);

  delay(300);


  // --------------------------------------------------------
  // Configure drivers
  // --------------------------------------------------------

  if (USE_UART_X) {

    configureDriver(
      driverX,
      X_CURRENT_MA,
      X_MICROSTEPS,
      USE_SPREADCYCLE_X
    );
  }


  if (USE_UART_Y1) {

    configureDriver(
      driverY1,
      Y_CURRENT_MA,
      Y_MICROSTEPS,
      USE_SPREADCYCLE_Y
    );
  }


  if (USE_UART_Y2) {

    configureDriver(
      driverY2,
      Y_CURRENT_MA,
      Y_MICROSTEPS,
      USE_SPREADCYCLE_Y
    );
  }


  if (USE_UART_Z) {

    configureDriver(
      driverZ,
      Z_CURRENT_MA,
      Z_MICROSTEPS,
      USE_SPREADCYCLE_Z
    );
  }


  if (USE_UART_P) {

    configureDriver(
      driverP,
      P_CURRENT_MA,
      P_MICROSTEPS,
      USE_SPREADCYCLE_P
    );
  }


  // --------------------------------------------------------
  // Configure motion
  // --------------------------------------------------------

  setupStepper(
    stepperX,
    TEST_MAX_SPEED_X,
    TEST_ACCEL_X,
    false
  );


  setupStepper(
    stepperY,
    TEST_MAX_SPEED_Y,
    TEST_ACCEL_Y,
    false
  );


  setupStepper(
    stepperZ,
    TEST_MAX_SPEED_Z,
    TEST_ACCEL_Z,
    false
  );


  // P direction inverted
  setupStepper(
    stepperP,
    TEST_MAX_SPEED_P,
    TEST_ACCEL_P,
    true
  );


  Serial.println("<ready>");
}


// ============================================================
// MAIN LOOP
// ============================================================

void loop() {

  recvWithStartEndMarkers();

  if (newData) {

    strcpy(
      tempChars,
      receivedChars
    );

    handleCommand();

    newData = false;
  }
}


// ============================================================
// Receive one <...> command
// ============================================================

void recvWithStartEndMarkers() {

  static boolean recvInProgress = false;

  static byte ndx = 0;

  const char startMarker = '<';

  const char endMarker = '>';

  char rc;


  while (
    Serial.available() > 0 &&
    !newData
  ) {

    rc = Serial.read();


    if (recvInProgress) {

      if (rc != endMarker) {

        receivedChars[ndx++] = rc;

        if (ndx >= numChars) {

          ndx = numChars - 1;
        }

      }
      else {

        receivedChars[ndx] = '\0';

        recvInProgress = false;

        ndx = 0;

        newData = true;
      }

    }
    else if (rc == startMarker) {

      recvInProgress = true;
    }
  }
}


// ============================================================
// Command dispatcher
// ============================================================

void handleCommand() {

  // --------------------------------------------------------
  // READY / PING
  // --------------------------------------------------------

  if (tempChars[0] == '?') {

    Serial.println("<ready>");

    return;
  }


  // --------------------------------------------------------
  // VERSION / SETTINGS
  // --------------------------------------------------------

  if (tempChars[0] == 'V') {

    printSettings();

    return;
  }


  // --------------------------------------------------------
  // Y DRIVER DIAGNOSTICS
  // --------------------------------------------------------

  if (tempChars[0] == 'D') {

    printYDiagnostics();

    return;
  }


  // --------------------------------------------------------
  // Motion command
  // --------------------------------------------------------

  long dx = 0;

  long dy = 0;

  long dz = 0;

  long dp = 0;

  bool anyMove = false;


  char *tok = strtok(
                tempChars,
                " "
              );


  if (tok == NULL) {

    return;
  }


  if (strcmp(tok, "M") != 0) {

    Serial.println(
      "<err unknown_cmd>"
    );

    return;
  }


  // --------------------------------------------------------
  // Parse axes
  // --------------------------------------------------------

  tok = strtok(
          NULL,
          " "
        );


  while (tok != NULL) {

    char axis = tok[0];

    long val = atol(
                 tok + 1
               );


    switch (axis) {

      case 'X':

        dx = val;

        anyMove = true;

        break;


      case 'Y':

        dy = val;

        anyMove = true;

        break;


      case 'Z':

        dz = val;

        anyMove = true;

        break;


      case 'P':

        dp = val;

        anyMove = true;

        break;


      default:

        Serial.println(
          "<err bad_axis>"
        );

        break;
    }


    tok = strtok(
            NULL,
            " "
          );
  }


  // --------------------------------------------------------
  // No movement
  // --------------------------------------------------------

  if (!anyMove) {

    Serial.println("<ok>");

    return;
  }


  // --------------------------------------------------------
  // Relative moves
  // --------------------------------------------------------

  stepperX.move(dx);

  stepperY.move(dy);

  stepperZ.move(dz);

  stepperP.move(dp);


  // --------------------------------------------------------
  // Run all axes until complete
  // --------------------------------------------------------

  while (

    stepperX.distanceToGo() != 0 ||

    stepperY.distanceToGo() != 0 ||

    stepperZ.distanceToGo() != 0 ||

    stepperP.distanceToGo() != 0

  ) {

    stepperX.run();

    stepperY.run();

    stepperZ.run();

    stepperP.run();
  }


  Serial.println("<ok>");
}
