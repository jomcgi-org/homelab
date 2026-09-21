package snapshotmeta

import (
	"os"
	"path/filepath"
	"testing"
)

func TestReadDeviceSetProducerShapesAndLegacyAbsence(t *testing.T) {
	for _, tc := range []struct {
		name string
		data string
		want DeviceSet
	}{
		{name: "root only", data: `[{"role":"rootfs","host_path":"/root"}]`, want: DeviceSet{Known: true}},
		{name: "placeholder", data: `[{"role":"volume","host_path":"/placeholder"},{"role":"rootfs","host_path":"/root"}]`, want: DeviceSet{Known: true, IDs: []string{"volume"}}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dir := t.TempDir()
			if err := os.WriteFile(filepath.Join(dir, JailResourcesFile), []byte(tc.data), 0o600); err != nil {
				t.Fatal(err)
			}
			got, err := ReadDeviceSet(dir)
			if err != nil {
				t.Fatal(err)
			}
			if !got.Equal(tc.want) {
				t.Fatalf("ReadDeviceSet() = %+v, want %+v", got, tc.want)
			}
		})
	}

	got, err := ReadDeviceSet(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	if got.Known || got.IDs != nil {
		t.Fatalf("legacy absence = %+v, want unknown", got)
	}
}
