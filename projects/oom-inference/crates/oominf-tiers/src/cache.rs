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
}
