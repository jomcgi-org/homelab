package idle

import (
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/agent_platform/vsockproto"
)

func TestNotIdleWhileCallInFlight(t *testing.T) {
	now := time.Unix(1000, 0)
	d := &Detector{IdleAfter: 10 * time.Second, Now: func() time.Time { return now }}
	d.Touch()
	d.Begin() // an in-flight model call opens

	now = now.Add(time.Hour) // lots of wall time passes
	if idle, _ := d.Evaluate(); idle {
		t.Fatal("must not be idle while a call is in flight (would snapshot mid-call)")
	}

	d.End()                  // call completes
	now = now.Add(time.Hour) // quiescent long enough
	if idle, _ := d.Evaluate(); !idle {
		t.Fatal("should be idle once quiescent past IdleAfter")
	}
}

func TestIdleRequiresIdleAfterElapsed(t *testing.T) {
	now := time.Unix(2000, 0)
	d := &Detector{IdleAfter: 30 * time.Second, Now: func() time.Time { return now }}
	d.End() // marks lastActivity = now, quiescent

	now = now.Add(10 * time.Second)
	if idle, _ := d.Evaluate(); idle {
		t.Fatal("should not be idle before IdleAfter elapses")
	}
	now = now.Add(25 * time.Second)
	if idle, _ := d.Evaluate(); !idle {
		t.Fatal("should be idle after IdleAfter elapses")
	}
}

func TestIdleFiresOnceUntilReArmed(t *testing.T) {
	now := time.Unix(3000, 0)
	d := &Detector{IdleAfter: time.Second, Now: func() time.Time { return now }}
	d.End()
	now = now.Add(5 * time.Second)

	if idle, _ := d.Evaluate(); !idle {
		t.Fatal("first evaluate should report idle")
	}
	if idle, _ := d.Evaluate(); idle {
		t.Fatal("second evaluate should not re-report idle without new activity")
	}
	// New activity re-arms.
	d.Touch()
	now = now.Add(5 * time.Second)
	if idle, _ := d.Evaluate(); !idle {
		t.Fatal("after new activity + quiescence, idle should report again")
	}
}

func TestCPUGate(t *testing.T) {
	now := time.Unix(4000, 0)
	cpuBusy := true
	d := &Detector{
		IdleAfter: time.Second,
		Now:       func() time.Time { return now },
		CPUIdle:   func() bool { return !cpuBusy },
	}
	d.End()
	now = now.Add(5 * time.Second)
	if idle, _ := d.Evaluate(); idle {
		t.Fatal("CPU busy should block idle even when quiescent")
	}
	cpuBusy = false
	if idle, _ := d.Evaluate(); !idle {
		t.Fatal("CPU idle should allow idle")
	}
}

func TestWakeConditionReported(t *testing.T) {
	now := time.Unix(5000, 0)
	d := &Detector{IdleAfter: time.Second, Now: func() time.Time { return now }}
	d.SetWakeCondition(vsockproto.WakeDiscordReply)
	d.End()
	now = now.Add(5 * time.Second)
	idle, wake := d.Evaluate()
	if !idle || wake != vsockproto.WakeDiscordReply {
		t.Fatalf("idle=%v wake=%q, want true/discord_reply", idle, wake)
	}
}
