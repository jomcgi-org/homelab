package driver

import "testing"

func TestBrickCgroupPath(t *testing.T) {
	for _, tc := range []struct{ in, want string }{
		{"/kubepods.slice/cri-containerd-abc.scope", "/kubepods.slice/cri-containerd-abc.scope"},
		{"/kubepods.slice/cri-containerd-abc.scope/embervm-noded-daemon", "/kubepods.slice/cri-containerd-abc.scope"},
		{"/kubepods.slice/cri-containerd-abc.scope/embervm-noded-daemon/", "/kubepods.slice/cri-containerd-abc.scope"},
		// Private cgroup namespace: the brick IS the namespace root.
		{"/embervm-noded-daemon", "/"},
		{"embervm-noded-daemon", "/"},
		{"/", "/"},
		{"", ""},
		// Only a trailing daemon child is the delegation layout.
		{"/embervm-noded-daemon/other", "/embervm-noded-daemon/other"},
		{"/a/embervm-vms", "/a/embervm-vms"},
	} {
		if got := BrickCgroupPath(tc.in); got != tc.want {
			t.Errorf("BrickCgroupPath(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}
}
