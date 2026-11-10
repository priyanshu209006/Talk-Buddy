// Captures mic audio at the context rate, resamples to the target rate (16 kHz wideband,
// or 8 kHz in phone mode) and emits Int16 PCM in 1024-byte frames.
class PhoneLineCapture extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const target = (options.processorOptions && options.processorOptions.target) || 16000;
    this.ratio = Math.max(1, sampleRate / target);
    this.acc = 0;
    this.sum = 0;
    this.count = 0;
    this.out = new Int16Array(512);   // 512 samples = 1024 bytes = 64 ms
    this.n = 0;
    this.muted = false;
    this.port.onmessage = e => { if ('muted' in e.data) this.muted = e.data.muted; };
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) {
      this.sum += ch[i];
      this.count++;
      this.acc += 1;
      if (this.acc >= this.ratio) {
        this.acc -= this.ratio;
        let s = this.muted ? 0 : this.sum / this.count;
        this.sum = 0; this.count = 0;
        s = Math.max(-1, Math.min(1, s));
        this.out[this.n++] = s < 0 ? s * 0x8000 : s * 0x7fff;
        if (this.n === this.out.length) {
          this.port.postMessage(this.out.buffer.slice(0));
          this.n = 0;
        }
      }
    }
    return true;
  }
}
registerProcessor('phone-line-capture', PhoneLineCapture);
