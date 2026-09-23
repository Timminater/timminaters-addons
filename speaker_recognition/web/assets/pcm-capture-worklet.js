"use strict";

class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.blockSize = 4096;
    this.pending = new Float32Array(this.blockSize);
    this.pendingLength = 0;
    this.port.onmessage = (event) => {
      if (event.data?.type !== "flush") return;
      if (this.pendingLength) {
        const tail = this.pending.slice(0, this.pendingLength);
        this.port.postMessage({ type: "samples", buffer: tail.buffer }, [tail.buffer]);
        this.pending = new Float32Array(this.blockSize);
        this.pendingLength = 0;
      }
      this.port.postMessage({ type: "flushed" });
    };
  }

  process(inputs, outputs) {
    const input = inputs[0];
    if (input?.length) {
      const frames = input[0].length;
      for (let frame = 0; frame < frames; frame += 1) {
        let sample = 0;
        for (let channel = 0; channel < input.length; channel += 1) sample += input[channel][frame] || 0;
        this.pending[this.pendingLength++] = sample / input.length;
        if (this.pendingLength === this.blockSize) {
          const block = this.pending;
          this.port.postMessage({ type: "samples", buffer: block.buffer }, [block.buffer]);
          this.pending = new Float32Array(this.blockSize);
          this.pendingLength = 0;
        }
      }
    }
    for (const output of outputs) for (const channel of output) channel.fill(0);
    return true;
  }
}

registerProcessor("pcm-capture", PcmCaptureProcessor);
