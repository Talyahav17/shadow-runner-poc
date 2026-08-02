       IDENTIFICATION DIVISION.
       PROGRAM-ID. GCD-CALC.

      * Euclidean algorithm for greatest common divisor. LINKAGE
      * SECTION field names (L-A, L-B, RESULT) and PIC S9(10) signed
      * integer signature are adapted from COBOLEval's
      * HumanEval/13 (greatest_common_divisor), MIT licensed:
      * https://github.com/zorse-project/COBOLEval
      *
      * COBOLEval's original LINKAGE SECTION groups the three fields
      * under one 01-level item (01 LINKED-ITEMS with 05 sub-fields,
      * passed as a single struct pointer) -- a different COBOL
      * calling convention than this bridge's generic ctypes proxy
      * currently supports (one buffer pointer per parameter). The
      * fields are declared here as separate flat 01-level items
      * instead, to fit that existing convention; the PROCEDURE
      * DIVISION body (the actual algorithm) is original, since
      * COBOLEval ships only a fill-in-the-blank stub with no COBOL
      * solution -- the Euclidean algorithm itself is public domain.

       ENVIRONMENT DIVISION.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-A     PIC S9(10).
       01  WS-B     PIC S9(10).
       01  WS-TEMP  PIC S9(10).

       LINKAGE SECTION.
       01  L-A      PIC S9(10).
       01  L-B      PIC S9(10).
       01  RESULT   PIC S9(10).

       PROCEDURE DIVISION USING L-A L-B RESULT.

       MAIN-LOGIC.
           MOVE FUNCTION ABS(L-A) TO WS-A
           MOVE FUNCTION ABS(L-B) TO WS-B
           PERFORM UNTIL WS-B = 0
               COMPUTE WS-TEMP = FUNCTION MOD(WS-A, WS-B)
               MOVE WS-B TO WS-A
               MOVE WS-TEMP TO WS-B
           END-PERFORM
           MOVE WS-A TO RESULT
           GOBACK.

       END PROGRAM GCD-CALC.
