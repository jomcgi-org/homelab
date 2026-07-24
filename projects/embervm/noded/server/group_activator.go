package server

import (
	"context"
	"errors"
	"fmt"
	"net"
	"sort"
	"strconv"
	"sync"
	"time"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"

	"github.com/jomcgi/homelab/projects/embervm/noded/substrate"
)

type groupActivatorFlight struct {
	done  chan struct{}
	entry *groupMemberEntry
	err   error
}

type groupRelightMember struct {
	plan   groupMemberPlanEntry
	bundle groupBundleEntry
}

// groupActivator is noded's node-local opaque-L4 composite wake path. The
// accepted port identifies the workload, so each eligible workload has exactly
// one in-flight complete-set relight and its parked connections independently
// splice once the entry member is ready.
type groupActivator struct {
	server *Server

	mu      sync.Mutex
	flights map[string]*groupActivatorFlight
	boots   int
	parked  map[string]int
	wakes   []time.Time
}

func newGroupActivator(s *Server) *groupActivator {
	return &groupActivator{
		server:  s,
		flights: make(map[string]*groupActivatorFlight),
		parked:  make(map[string]int),
	}
}

// StartGroupActivator starts one accept loop for every already-bound composite
// L4 listener. Listeners are closed when ctx is cancelled so blocked Accept
// calls exit with daemon shutdown.
func (s *Server) StartGroupActivator(ctx context.Context, listeners []net.Listener) {
	if s.groupActivator == nil || len(listeners) == 0 {
		return
	}
	go func() {
		<-ctx.Done()
		for _, lis := range listeners {
			_ = lis.Close()
		}
	}()
	for _, lis := range listeners {
		port := listenerPort(lis)
		if port == 0 {
			s.logger.Warn("group activator: listener has no TCP port", "addr", lis.Addr().String())
			_ = lis.Close()
			continue
		}
		go s.groupActivator.serve(ctx, lis, port)
	}
}

func (a *groupActivator) serve(ctx context.Context, lis net.Listener, port uint32) {
	a.server.logger.Info("group activator listener listening", "addr", lis.Addr().String(), "port", port)
	for {
		conn, err := lis.Accept()
		if err != nil {
			if ctx.Err() != nil || errors.Is(err, net.ErrClosed) {
				return
			}
			a.server.logger.Warn("group activator: accept failed", "port", port, "err", err)
			continue
		}
		go a.handle(ctx, conn, port)
	}
}

func (a *groupActivator) handle(ctx context.Context, conn net.Conn, listenPort uint32) {
	reg, ok := a.server.registry.groupByListenPort(listenPort)
	if !ok {
		a.server.logger.Warn("group activator: no eligible workload for listener", "port", listenPort)
		_ = conn.Close()
		return
	}
	entryPlan, err := groupEntryPlan(reg.GroupMemberPlan)
	if err != nil {
		a.server.logger.Warn("group activator: invalid member plan", "workload", reg.Workload, "err", err)
		_ = conn.Close()
		return
	}

	if live, ok := a.liveEntry(reg.Workload, entryPlan.MemberName, reg.GroupMemberPlan); ok {
		a.splice(ctx, conn, live)
		return
	}

	flight, leader, ok := a.join(reg.Workload)
	if !ok {
		a.server.logger.Warn("group activator: wake rejected by local limit", "workload", reg.Workload)
		_ = conn.Close()
		return
	}
	defer a.unpark(reg.Workload)

	if leader {
		entry, wakeErr := a.wake(ctx, reg)
		a.complete(reg.Workload, flight, entry, wakeErr)
	}
	select {
	case <-flight.done:
	case <-ctx.Done():
		_ = conn.Close()
		return
	}
	if flight.err != nil || flight.entry == nil {
		a.server.logger.Warn("group activator: wake failed", "workload", reg.Workload, "err", flight.err)
		_ = conn.Close()
		return
	}
	a.splice(ctx, conn, flight.entry)
}

// join accounts for one parked connection. A follower joins an existing
// workload flight; a leader reserves a globally bounded boot slot and a local
// wake-rate slot.
func (a *groupActivator) join(workload string) (*groupActivatorFlight, bool, bool) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.parked[workload] >= activatorMaxParked {
		return nil, false, false
	}
	if f, ok := a.flights[workload]; ok {
		a.parked[workload]++
		return f, false, true
	}
	if a.boots >= activatorMaxBoots || !a.allowWakeLocked(time.Now()) {
		return nil, false, false
	}
	f := &groupActivatorFlight{done: make(chan struct{})}
	a.flights[workload] = f
	a.boots++
	a.parked[workload]++
	return f, true, true
}

func (a *groupActivator) allowWakeLocked(now time.Time) bool {
	cutoff := now.Add(-activatorWakeWindow)
	kept := a.wakes[:0]
	for _, woke := range a.wakes {
		if woke.After(cutoff) {
			kept = append(kept, woke)
		}
	}
	a.wakes = kept
	if len(a.wakes) >= activatorWakeMax {
		return false
	}
	a.wakes = append(a.wakes, now)
	return true
}

func (a *groupActivator) unpark(workload string) {
	a.mu.Lock()
	if a.parked[workload] <= 1 {
		delete(a.parked, workload)
	} else {
		a.parked[workload]--
	}
	a.mu.Unlock()
}

func (a *groupActivator) complete(workload string, flight *groupActivatorFlight, entry *groupMemberEntry, err error) {
	a.mu.Lock()
	flight.entry = entry
	flight.err = err
	delete(a.flights, workload)
	a.boots--
	close(flight.done)
	a.mu.Unlock()
}

func (a *groupActivator) wake(ctx context.Context, reg workloadEntry) (*groupMemberEntry, error) {
	if entryPlan, err := groupEntryPlan(reg.GroupMemberPlan); err == nil {
		if live, ok := a.liveEntry(reg.Workload, entryPlan.MemberName, reg.GroupMemberPlan); ok {
			return live, nil
		}
	}

	members, groupInstanceID, err := a.completeLocalSet(reg.GroupMemberPlan)
	if err != nil {
		return nil, err
	}
	record, ok := a.groupNetworkRecord(groupInstanceID)
	if !ok || record.SubnetCIDR == "" {
		return nil, fmt.Errorf("noded: group %q has no persisted network record", groupInstanceID)
	}
	if _, err := a.server.CreateGroupNetwork(ctx, &nodev1.CreateGroupNetworkRequest{
		Trace:           &nodev1.Trace{Workload: reg.Workload},
		GroupInstanceId: groupInstanceID,
		Cidr:            record.SubnetCIDR,
	}); err != nil {
		return nil, err
	}

	sort.Slice(members, func(i, j int) bool {
		if members[i].plan.StartOrder == members[j].plan.StartOrder {
			return members[i].plan.MemberName < members[j].plan.MemberName
		}
		return members[i].plan.StartOrder < members[j].plan.StartOrder
	})

	var started []string
	var entry *groupMemberEntry
	for first := 0; first < len(members); {
		last := first + 1
		for last < len(members) && members[last].plan.StartOrder == members[first].plan.StartOrder {
			last++
		}
		type result struct {
			member groupRelightMember
			resp   *nodev1.StartGroupMemberResponse
			err    error
		}
		results := make(chan result, last-first)
		for _, member := range members[first:last] {
			member := member
			go func() {
				resp, startErr := a.server.startGroupMember(ctx, &nodev1.StartGroupMemberRequest{
					Trace:           &nodev1.Trace{Workload: reg.Workload},
					Mode:            nodev1.StartGroupMemberMode_START_GROUP_MEMBER_MODE_RELIGHT,
					GroupInstanceId: groupInstanceID,
					MemberName:      member.plan.MemberName,
					MemberIndex:     member.plan.MemberIndex,
					Ip:              member.bundle.pinnedIP,
					SnapshotRef:     member.bundle.snapshotRef,
					HealthPort:      member.plan.HealthPort,
					Resources: &nodev1.ResourceSpec{
						Vcpus:  member.plan.VCPUs,
						MemMib: member.plan.MemMib,
					},
					EntryGuestPort: member.plan.EntryGuestPort,
				}, nodev1.InstanceOrigin_INSTANCE_ORIGIN_ACTIVATOR)
				results <- result{member: member, resp: resp, err: startErr}
			}()
		}

		var tierErr error
		for n := first; n < last; n++ {
			result := <-results
			if result.err != nil {
				if tierErr == nil {
					tierErr = fmt.Errorf("noded: relight group member %q: %w", result.member.plan.MemberName, result.err)
				}
				continue
			}
			started = append(started, result.resp.GetVmId())
			if result.member.plan.EntryGuestPort > 0 {
				entry = a.server.groupMembers.get(result.resp.GetVmId())
			}
		}
		if tierErr != nil {
			a.reapStarted(started)
			return nil, tierErr
		}
		first = last
	}
	if entry == nil || entry.entryGuestPort == 0 {
		a.reapStarted(started)
		return nil, fmt.Errorf("noded: relit group %q has no live entry member", groupInstanceID)
	}
	return entry, nil
}

func groupEntryPlan(plan []groupMemberPlanEntry) (groupMemberPlanEntry, error) {
	var entry groupMemberPlanEntry
	found := false
	for _, member := range plan {
		if member.EntryGuestPort == 0 {
			continue
		}
		if found {
			return groupMemberPlanEntry{}, fmt.Errorf("multiple entry members")
		}
		entry = member
		found = true
	}
	if !found {
		return groupMemberPlanEntry{}, fmt.Errorf("entry member missing")
	}
	return entry, nil
}

func (a *groupActivator) liveEntry(workload, entryMember string, plan []groupMemberPlanEntry) (*groupMemberEntry, bool) {
	if live, ok := a.server.groupMembers.entryByWorkload(workload, entryMember); ok {
		return live, true
	}
	expected := make(map[string]struct{}, len(plan))
	for _, member := range plan {
		expected[member.MemberName] = struct{}{}
	}
	groupIDs := make(map[string]struct{})
	for _, bundle := range a.server.groupBundles.snapshot() {
		if _, ok := expected[bundle.memberName]; ok && bundle.groupInstanceID != "" {
			groupIDs[bundle.groupInstanceID] = struct{}{}
		}
	}
	if len(groupIDs) != 1 {
		return nil, false
	}
	for groupInstanceID := range groupIDs {
		return a.server.groupMembers.entryByGroup(groupInstanceID, entryMember)
	}
	return nil, false
}

// completeLocalSet enforces the node-local relight boundary. Every planned
// member must map to exactly one local bundle, all bundles must name one set and
// group instance, and every bundle must carry post-migration pinned-IP metadata.
func (a *groupActivator) completeLocalSet(plan []groupMemberPlanEntry) ([]groupRelightMember, string, error) {
	if len(plan) == 0 {
		return nil, "", fmt.Errorf("noded: group member plan is empty")
	}
	expected := make(map[string]groupMemberPlanEntry, len(plan))
	for _, member := range plan {
		if member.MemberName == "" {
			return nil, "", fmt.Errorf("noded: group member plan contains an empty member name")
		}
		if _, exists := expected[member.MemberName]; exists {
			return nil, "", fmt.Errorf("noded: group member plan repeats member %q", member.MemberName)
		}
		expected[member.MemberName] = member
	}
	found := make(map[string]groupBundleEntry, len(plan))
	for _, bundle := range a.server.groupBundles.snapshot() {
		if _, wanted := expected[bundle.memberName]; !wanted {
			continue
		}
		if prior, exists := found[bundle.memberName]; exists {
			return nil, "", fmt.Errorf("noded: member %q has bundles in multiple local sets %q and %q", bundle.memberName, prior.setID, bundle.setID)
		}
		found[bundle.memberName] = bundle
	}

	members := make([]groupRelightMember, 0, len(plan))
	var setID, groupInstanceID string
	for _, member := range plan {
		bundle, ok := found[member.MemberName]
		if !ok {
			return nil, "", fmt.Errorf("noded: complete local group set missing member %q", member.MemberName)
		}
		if bundle.pinnedIP == "" {
			return nil, "", fmt.Errorf("noded: group member %q lacks pinned-IP bank metadata", member.MemberName)
		}
		if net.ParseIP(bundle.pinnedIP) == nil {
			return nil, "", fmt.Errorf("noded: group member %q has invalid pinned IP %q", member.MemberName, bundle.pinnedIP)
		}
		if bundle.setID == "" || bundle.groupInstanceID == "" {
			return nil, "", fmt.Errorf("noded: group member %q lacks set or group identity", member.MemberName)
		}
		if setID == "" {
			setID = bundle.setID
			groupInstanceID = bundle.groupInstanceID
		}
		if bundle.setID != setID || bundle.groupInstanceID != groupInstanceID {
			return nil, "", fmt.Errorf("noded: local group bundles span multiple sets or group instances")
		}
		members = append(members, groupRelightMember{plan: member, bundle: bundle})
	}
	return members, groupInstanceID, nil
}

func (a *groupActivator) groupNetworkRecord(groupInstanceID string) (substrate.GroupNetworkRecord, bool) {
	if a.server.groupRecords == nil {
		return substrate.GroupNetworkRecord{}, false
	}
	for _, record := range a.server.groupRecords.ScanGroupNetworks() {
		if record.GroupInstanceID == groupInstanceID {
			return record, true
		}
	}
	return substrate.GroupNetworkRecord{}, false
}

func (a *groupActivator) reapStarted(vmIDs []string) {
	for _, vmID := range vmIDs {
		if _, err := a.server.StopGroupMember(context.Background(), &nodev1.StopGroupMemberRequest{
			VmId: vmID,
			Mode: nodev1.StopGroupMemberMode_STOP_GROUP_MEMBER_MODE_DESTROY,
		}); err != nil {
			a.server.logger.Warn("group activator: reap failed relight member", "vm", vmID, "err", err)
		}
	}
}

func (a *groupActivator) splice(ctx context.Context, client net.Conn, entry *groupMemberEntry) {
	if entry == nil || entry.ip == nil || entry.entryGuestPort == 0 {
		_ = client.Close()
		return
	}
	guest, err := (&net.Dialer{}).DialContext(ctx, "tcp", net.JoinHostPort(entry.ip.String(), strconv.FormatUint(uint64(entry.entryGuestPort), 10)))
	if err != nil {
		a.server.logger.Warn("group activator: entry dial failed", "workload", entry.workload, "err", err)
		_ = client.Close()
		return
	}
	defer client.Close()
	defer guest.Close()

	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		pumpTCP(guest, client)
	}()
	go func() {
		defer wg.Done()
		pumpTCP(client, guest)
	}()
	wg.Wait()
}
