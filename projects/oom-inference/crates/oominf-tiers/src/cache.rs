//! Placement for one tier: a fixed set of slots, a key-to-slot map and a policy.
//! GPU-free, so the same code drives the real tiers and trace replays.

use std::collections::HashMap;

use crate::policy::Policy;

pub struct SlotCache {
    capacity: usize,
    slot_of: HashMap<u32, usize>,
    key_of: Vec<Option<u32>>,
    free: Vec<usize>,
    policy: Box<dyn Policy>,
    clock: u64,
}

/// Outcome of placing a key.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Place {
    /// Already resident in this slot.
    Hit(usize),
    /// Newly assigned this slot, evicting the given key if any. The caller must fill it.
    Miss(usize, Option<u32>),
}

impl SlotCache {
    pub fn new(capacity: usize, policy: Box<dyn Policy>) -> Self {
        SlotCache {
            capacity,
            slot_of: HashMap::with_capacity(capacity),
            key_of: vec![None; capacity],
            free: (0..capacity).rev().collect(),
            policy,
            clock: 0,
        }
    }

    pub fn capacity(&self) -> usize {
        self.capacity
    }

    pub fn policy_name(&self) -> String {
        self.policy.name()
    }

    /// Slot holding `key`, without counting an access.
    pub fn peek(&self, key: u32) -> Option<usize> {
        self.slot_of.get(&key).copied()
    }

    /// Places `key`, counting an access. `pinned(k)` keys are never evicted; returns
    /// `None` when every slot is pinned.
    pub fn place(&mut self, key: u32, pinned: &dyn Fn(u32) -> bool) -> Option<Place> {
        self.clock += 1;
        if let Some(&slot) = self.slot_of.get(&key) {
            self.policy.touch(key, self.clock);
            return Some(Place::Hit(slot));
        }
        let (slot, evicted) = match self.free.pop() {
            Some(s) => (s, None),
            None => {
                let victim = self.policy.victim(pinned)?;
                let s = self.slot_of.remove(&victim).expect("policy and map agree");
                self.policy.remove(victim);
                (s, Some(victim))
            }
        };
        self.slot_of.insert(key, slot);
        self.key_of[slot] = Some(key);
        self.policy.touch(key, self.clock);
        Some(Place::Miss(slot, evicted))
    }

    /// Whether a slot is free, so placing a new key would evict nothing.
    pub fn has_free(&self) -> bool {
        !self.free.is_empty()
    }

    /// Drops `key` (e.g. its fill failed).
    pub fn forget(&mut self, key: u32) {
        if let Some(slot) = self.slot_of.remove(&key) {
            self.key_of[slot] = None;
            self.policy.remove(key);
            self.free.push(slot);
        }
    }

    /// Retires every slot at or above `capacity`, dropping their keys, and returns
    /// the dropped keys. The caller must make sure nothing still reads those slots.
    pub fn shrink(&mut self, capacity: usize) -> Vec<u32> {
        let capacity = capacity.min(self.capacity);
        let dropped: Vec<u32> = self.key_of[capacity..].iter().flatten().copied().collect();
        for &k in &dropped {
            self.slot_of.remove(&k);
            self.policy.remove(k);
        }
        self.key_of.truncate(capacity);
        self.free.retain(|&s| s < capacity);
        self.capacity = capacity;
        dropped
    }

    /// Adds empty slots up to `capacity`.
    pub fn grow(&mut self, capacity: usize) {
        if capacity <= self.capacity {
            return;
        }
        self.key_of.resize(capacity, None);
        self.free.extend((self.capacity..capacity).rev());
        self.capacity = capacity;
    }

    pub fn len(&self) -> usize {
        self.slot_of.len()
    }

    pub fn is_empty(&self) -> bool {
        self.slot_of.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::policy::Lru;

    #[test]
    fn fills_then_evicts_unpinned() {
        let mut c = SlotCache::new(2, Box::new(Lru::default()));
        let none = |_: u32| false;
        assert_eq!(c.place(1, &none), Some(Place::Miss(0, None)));
        assert_eq!(c.place(2, &none), Some(Place::Miss(1, None)));
        assert_eq!(c.place(1, &none), Some(Place::Hit(0)));
        // 2 is LRU but pinned, so 1 goes.
        assert_eq!(c.place(3, &|k| k == 2), Some(Place::Miss(0, Some(1))));
        assert_eq!(c.place(4, &|k| k == 2 || k == 3), None);
        assert_eq!(c.peek(3), Some(0));
        c.forget(3);
        assert_eq!(c.place(4, &|_| false), Some(Place::Miss(0, None)));
    }

    #[test]
    fn shrink_drops_top_slots_only() {
        let mut c = SlotCache::new(4, Box::new(Lru::default()));
        let none = |_: u32| false;
        for k in 10..13 {
            c.place(k, &none);
        }
        // Slots 0, 1, 2 hold 10, 11, 12; slot 3 is free.
        assert_eq!(c.shrink(2), vec![12]);
        assert_eq!((c.capacity(), c.len()), (2, 2));
        assert_eq!(c.peek(12), None);
        assert!(!c.has_free());
        // The policy forgot 12 too: the next miss evicts a surviving key.
        assert_eq!(c.place(13, &none), Some(Place::Miss(0, Some(10))));
        // Growing back adds free slots that fill before anything is evicted.
        c.grow(3);
        assert_eq!(c.place(14, &none), Some(Place::Miss(2, None)));
    }
}
