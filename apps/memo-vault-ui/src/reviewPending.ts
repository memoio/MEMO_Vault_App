export class ReviewPending {
  private ids = new Set<string>();
  has(id: string) { return this.ids.has(id); }
  start(id: string) {
    if (this.ids.has(id)) return false;
    this.ids.add(id);
    return true;
  }
  finish(id: string) { this.ids.delete(id); }
  snapshot() { return new Set(this.ids); }
}
