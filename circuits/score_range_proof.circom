pragma circom 2.1.0;

include "circomlib/circuits/comparators.circom";
include "circomlib/circuits/bitify.circom";
include "constants.circom";

// Conditionally add a point (x2, y2) to (x1, y1) if bit is 1.
// If bit is 0, returns (x1, y1).
template Bn254CondAdd() {
    signal input x1;
    signal input y1;
    signal input x2;
    signal input y2;
    signal input bit;

    signal output x3;
    signal output y3;

    signal lambda;
    lambda <-- (y2 - y1) / (x2 - x1);
    lambda * (x2 - x1) === y2 - y1;

    signal x3_add;
    x3_add <-- lambda * lambda - x1 - x2;
    x3_add === lambda * lambda - x1 - x2;

    signal y3_add;
    y3_add <-- lambda * (x1 - x3_add) - y1;
    y3_add === lambda * (x1 - x3_add) - y1;

    x3 <-- x1 + bit * (x3_add - x1);
    y3 <-- y1 + bit * (y3_add - y1);
}

// Add two points on BN254 G1 natively
template Bn254Add() {
    signal input x1;
    signal input y1;
    signal input x2;
    signal input y2;
    signal output x3;
    signal output y3;

    signal lambda;
    lambda <-- (y2 - y1) / (x2 - x1);
    lambda * (x2 - x1) === y2 - y1;

    x3 <-- lambda * lambda - x1 - x2;
    x3 === lambda * lambda - x1 - x2;

    y3 <-- lambda * (x1 - x3) - y1;
    y3 === lambda * (x1 - x3) - y1;
}

template ScoreRangeProof(maxScore) {
    signal input score;          // private
    signal input blinding;       // private
    signal input commitX;        // public (Pedersen commitment x-coord)
    signal input commitY;        // public
    signal input threshold;      // public

    // 1. Bit decompose score (7 bits) and blinding (254 bits)
    component scoreBits = Num2Bits(7);
    scoreBits.in <== score;

    component blindingBits = Num2Bits(254);
    blindingBits.in <== blinding;

    // Dummy point D = 2 * G (precomputed in constants.circom as get_G_pow2(1))
    signal dx;
    signal dy;
    dx <-- get_G_pow2_x(1);
    dy <-- get_G_pow2_y(1);

    // 2. Accumulate G points for score
    signal score_x[8];
    signal score_y[8];
    score_x[0] <== dx;
    score_y[0] <== dy;

    component scoreAdd[7];
    for (var i = 0; i < 7; i++) {
        scoreAdd[i] = Bn254CondAdd();
        scoreAdd[i].x1 <== score_x[i];
        scoreAdd[i].y1 <== score_y[i];
        scoreAdd[i].x2 <-- get_G_pow2_x(i);
        scoreAdd[i].y2 <-- get_G_pow2_y(i);
        scoreAdd[i].bit <== scoreBits.out[i];
        score_x[i+1] <== scoreAdd[i].x3;
        score_y[i+1] <== scoreAdd[i].y3;
    }

    // 3. Accumulate H points for blinding
    signal blinding_x[255];
    signal blinding_y[255];
    blinding_x[0] <== score_x[7];
    blinding_y[0] <== score_y[7];

    component blindingAdd[254];
    for (var i = 0; i < 254; i++) {
        blindingAdd[i] = Bn254CondAdd();
        blindingAdd[i].x1 <== blinding_x[i];
        blindingAdd[i].y1 <== blinding_y[i];
        blindingAdd[i].x2 <-- get_H_pow2_x(i);
        blindingAdd[i].y2 <-- get_H_pow2_y(i);
        blindingAdd[i].bit <== blindingBits.out[i];
        blinding_x[i+1] <== blindingAdd[i].x3;
        blinding_y[i+1] <== blindingAdd[i].y3;
    }

    // 4. Verify C + D === Accumulator
    component finalAdd = Bn254Add();
    finalAdd.x1 <== commitX;
    finalAdd.y1 <== commitY;
    finalAdd.x2 <== dx;
    finalAdd.y2 <== dy;

    finalAdd.x3 === blinding_x[254];
    finalAdd.y3 === blinding_y[254];

    // 5. Enforce score >= threshold
    component geq = GreaterEqThan(8);
    geq.in[0] <== score;
    geq.in[1] <== threshold;
    geq.out === 1;
}

component main {public [commitX, commitY, threshold]} = ScoreRangeProof(100);
