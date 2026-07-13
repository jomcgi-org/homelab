package controller

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	v1alpha1 "github.com/jomcgi/homelab/projects/operators/oci-model-cache/api/v1alpha1"
	"github.com/jomcgi/homelab/projects/operators/oci-model-cache/internal/config"
	"github.com/jomcgi/homelab/projects/operators/oci-model-cache/internal/naming"
)

// minimalModelCache returns a ModelCache with the minimum required fields.
func minimalModelCache() *v1alpha1.ModelCache {
	return &v1alpha1.ModelCache{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-model",
			Namespace: "default",
		},
		Spec: v1alpha1.ModelCacheSpec{
			Repo:     "bartowski/Llama-3.2-1B-Instruct-GGUF",
			Registry: "ghcr.io/jomcgi/models",
			Revision: "main",
		},
		Status: v1alpha1.ModelCacheStatus{
			ResolvedRef: "ghcr.io/jomcgi/models/llama:main",
		},
	}
}

func minimalConfig() config.Config {
	return config.Config{
		Namespace: "oci-model-cache",
		CopyImage: "ghcr.io/jomcgi/homelab/bazel/tools/hf2oci:main",
	}
}

// TestBuildCopyJob_BasicStructure verifies the core Job fields are set correctly.
func TestBuildCopyJob_BasicStructure(t *testing.T) {
	mc := minimalModelCache()
	cfg := minimalConfig()

	job := buildCopyJob(mc, cfg)

	require.NotNil(t, job)
	assert.Equal(t, cfg.Namespace, job.Namespace)
	assert.Equal(t, naming.JobName(mc.Status.ResolvedRef), job.Name)

	// Labels
	assert.Equal(t, "oci-model-cache", job.Labels["app.kubernetes.io/name"])
	assert.Equal(t, "sync", job.Labels["app.kubernetes.io/component"])
	assert.Equal(t, "oci-model-cache-operator", job.Labels["app.kubernetes.io/managed-by"])
	assert.Equal(t, mc.Name, job.Labels["oci-model-cache.jomcgi.dev/modelcache"])

	// Spec
	require.NotNil(t, job.Spec.BackoffLimit)
	assert.Equal(t, int32(0), *job.Spec.BackoffLimit, "BackoffLimit must be 0 (operator controls retries)")
	require.NotNil(t, job.Spec.TTLSecondsAfterFinished)
	assert.Equal(t, int32(300), *job.Spec.TTLSecondsAfterFinished)
	assert.Equal(t, corev1.RestartPolicyNever, job.Spec.Template.Spec.RestartPolicy)
}

// TestBuildCopyJob_ContainerArgs verifies required CLI args are present.
func TestBuildCopyJob_ContainerArgs(t *testing.T) {
	mc := minimalModelCache()
	cfg := minimalConfig()

	job := buildCopyJob(mc, cfg)

	require.Len(t, job.Spec.Template.Spec.Containers, 1)
	container := job.Spec.Template.Spec.Containers[0]

	assert.Equal(t, "hf2oci", container.Name)
	assert.Equal(t, cfg.CopyImage, container.Image)
	assert.Equal(t, corev1.PullAlways, container.ImagePullPolicy)

	// Required arguments
	args := container.Args
	assert.Contains(t, args, "copy")
	assert.Contains(t, args, mc.Spec.Repo)
	assert.Contains(t, args, "--registry")
	assert.Contains(t, args, mc.Spec.Registry)
	assert.Contains(t, args, "-o")
	assert.Contains(t, args, "json")
	assert.Contains(t, args, "-O")
	assert.Contains(t, args, "/dev/termination-log")
}

// TestBuildCopyJob_OptionalArgs verifies optional spec fields produce the right args.
func TestBuildCopyJob_OptionalArgs(t *testing.T) {
	mc := minimalModelCache()
	mc.Spec.Revision = "abc123"
	mc.Spec.File = "Llama-3.2-1B-Instruct-Q4_K_M.gguf"
	mc.Spec.Tag = "custom-tag"
	mc.Spec.ModelDir = "/models/llama"
	cfg := minimalConfig()

	job := buildCopyJob(mc, cfg)
	args := job.Spec.Template.Spec.Containers[0].Args

	assertConsecutive(t, args, "--revision", "abc123")
	assertConsecutive(t, args, "--file", mc.Spec.File)
	assertConsecutive(t, args, "--tag", "custom-tag")
	assertConsecutive(t, args, "--model-dir", "/models/llama")
}

// TestBuildCopyJob_NoOptionalArgsWhenEmpty verifies omitted spec fields produce no extra args.
func TestBuildCopyJob_NoOptionalArgsWhenEmpty(t *testing.T) {
	mc := minimalModelCache()
	mc.Spec.Revision = "" // empty → no --revision arg
	cfg := minimalConfig()

	job := buildCopyJob(mc, cfg)
	args := job.Spec.Template.Spec.Containers[0].Args

	assert.NotContains(t, args, "--revision")
	assert.NotContains(t, args, "--file")
	assert.NotContains(t, args, "--tag")
	assert.NotContains(t, args, "--model-dir")
}

// TestBuildCopyJob_SecurityContext verifies the non-root security posture.
func TestBuildCopyJob_SecurityContext(t *testing.T) {
	mc := minimalModelCache()
	cfg := minimalConfig()

	job := buildCopyJob(mc, cfg)
	sc := job.Spec.Template.Spec.Containers[0].SecurityContext

	require.NotNil(t, sc)
	require.NotNil(t, sc.RunAsNonRoot)
	assert.True(t, *sc.RunAsNonRoot)
	require.NotNil(t, sc.AllowPrivilegeEscalation)
	assert.False(t, *sc.AllowPrivilegeEscalation)
	require.NotNil(t, sc.Capabilities)
	assert.Contains(t, sc.Capabilities.Drop, corev1.Capability("ALL"))
	require.NotNil(t, sc.SeccompProfile)
	assert.Equal(t, corev1.SeccompProfileTypeRuntimeDefault, sc.SeccompProfile.Type)
}

// TestBuildCopyJob_ResourceLimits verifies memory requests and limits are applied.
func TestBuildCopyJob_ResourceLimits(t *testing.T) {
	mc := minimalModelCache()
	cfg := minimalConfig()
	cfg.SyncMemoryRequest = "512Mi"
	cfg.SyncMemoryLimit = "2Gi"

	job := buildCopyJob(mc, cfg)
	resources := job.Spec.Template.Spec.Containers[0].Resources

	expectedRequest := resource.MustParse("512Mi")
	expectedLimit := resource.MustParse("2Gi")

	reqQ := resources.Requests[corev1.ResourceMemory]
	limQ := resources.Limits[corev1.ResourceMemory]
	assert.Equal(t, expectedRequest.Value(), reqQ.Value())
	assert.Equal(t, expectedLimit.Value(), limQ.Value())
}

// TestBuildCopyJob_GoMemLimitEnv verifies GOMEMLIMIT is set to 80% of the memory limit.
func TestBuildCopyJob_GoMemLimitEnv(t *testing.T) {
	mc := minimalModelCache()
	cfg := minimalConfig()
	cfg.SyncMemoryLimit = "1Gi" // 1 GiB = 1073741824 bytes; 80% = 858993459 bytes

	job := buildCopyJob(mc, cfg)
	envVars := job.Spec.Template.Spec.Containers[0].Env

	goMemLimit := findEnv(envVars, "GOMEMLIMIT")
	require.NotEmpty(t, goMemLimit, "GOMEMLIMIT env var should be set when SyncMemoryLimit is configured")

	// 80% of 1Gi
	limit := resource.MustParse("1Gi")
	expected := limit.Value() * 4 / 5
	assert.Equal(t, "858993459B", goMemLimit)
	_ = expected
}

// TestBuildCopyJob_MemoryLimitOnly verifies Limits without Requests doesn't set Requests.
func TestBuildCopyJob_MemoryLimitOnly(t *testing.T) {
	mc := minimalModelCache()
	cfg := minimalConfig()
	cfg.SyncMemoryLimit = "2Gi"

	job := buildCopyJob(mc, cfg)
	resources := job.Spec.Template.Spec.Containers[0].Resources

	assert.Nil(t, resources.Requests, "Requests should not be set when only Limit is configured")
	require.NotNil(t, resources.Limits)
}

// TestBuildCopyJob_NoResourcesWhenUnset verifies no resource requirements when not configured.
func TestBuildCopyJob_NoResourcesWhenUnset(t *testing.T) {
	mc := minimalModelCache()
	cfg := minimalConfig()
	// SyncMemoryRequest and SyncMemoryLimit are empty

	job := buildCopyJob(mc, cfg)
	resources := job.Spec.Template.Spec.Containers[0].Resources

	assert.Empty(t, resources.Requests)
	assert.Empty(t, resources.Limits)
}

// TestBuildCopyJob_MaxParallelArg verifies --max-parallel is added to args when configured.
func TestBuildCopyJob_MaxParallelArg(t *testing.T) {
	mc := minimalModelCache()
	cfg := minimalConfig()
	cfg.SyncMaxParallel = "8"

	job := buildCopyJob(mc, cfg)
	args := job.Spec.Template.Spec.Containers[0].Args

	assertConsecutive(t, args, "--max-parallel", "8")
}

// TestBuildCopyJob_NoMaxParallelWhenEmpty verifies --max-parallel is not added when unconfigured.
func TestBuildCopyJob_NoMaxParallelWhenEmpty(t *testing.T) {
	mc := minimalModelCache()
	cfg := minimalConfig()

	job := buildCopyJob(mc, cfg)
	args := job.Spec.Template.Spec.Containers[0].Args

	assert.NotContains(t, args, "--max-parallel")
}

// TestBuildCopyJob_NodeSelector verifies node selectors are applied when configured.
func TestBuildCopyJob_NodeSelector(t *testing.T) {
	mc := minimalModelCache()
	cfg := minimalConfig()
	cfg.SyncNodeSelector = map[string]string{
		"kubernetes.io/arch": "amd64",
		"workload-type":      "gpu",
	}

	job := buildCopyJob(mc, cfg)

	assert.Equal(t, cfg.SyncNodeSelector, job.Spec.Template.Spec.NodeSelector)
}

// TestBuildCopyJob_NoNodeSelectorWhenEmpty verifies no node selector when not configured.
func TestBuildCopyJob_NoNodeSelectorWhenEmpty(t *testing.T) {
	mc := minimalModelCache()
	cfg := minimalConfig()

	job := buildCopyJob(mc, cfg)

	assert.Nil(t, job.Spec.Template.Spec.NodeSelector)
}

// TestBuildCopyJob_ServiceAccount verifies SA is applied when configured.
func TestBuildCopyJob_ServiceAccount(t *testing.T) {
	mc := minimalModelCache()
	cfg := minimalConfig()
	cfg.SyncServiceAccount = "oci-model-cache-sync"

	job := buildCopyJob(mc, cfg)

	assert.Equal(t, "oci-model-cache-sync", job.Spec.Template.Spec.ServiceAccountName)
}

// TestBuildCopyJob_HFTokenSecret verifies the HF_TOKEN env var is injected.
func TestBuildCopyJob_HFTokenSecret(t *testing.T) {
	mc := minimalModelCache()
	cfg := minimalConfig()
	cfg.HFTokenSecret = "hf-token"
	cfg.HFTokenSecretKey = "token"

	job := buildCopyJob(mc, cfg)
	envVars := job.Spec.Template.Spec.Containers[0].Env

	hfToken := findEnvFromSecret(envVars, "HF_TOKEN")
	require.NotNil(t, hfToken, "HF_TOKEN should be injected from secret")
	assert.Equal(t, "hf-token", hfToken.Name)
	assert.Equal(t, "token", hfToken.Key)
}

// TestBuildCopyJob_RegistryPushSecret verifies volumes and mounts are added.
func TestBuildCopyJob_RegistryPushSecret(t *testing.T) {
	mc := minimalModelCache()
	cfg := minimalConfig()
	cfg.RegistryPushSecret = "registry-creds"

	job := buildCopyJob(mc, cfg)

	// Volume should be present
	volumes := job.Spec.Template.Spec.Volumes
	vol := findVolume(volumes, "registry-push-creds")
	require.NotNil(t, vol, "registry-push-creds volume should be present")
	assert.Equal(t, "registry-creds", vol.VolumeSource.Secret.SecretName)

	// VolumeMount should be present
	mounts := job.Spec.Template.Spec.Containers[0].VolumeMounts
	mount := findMount(mounts, "registry-push-creds")
	require.NotNil(t, mount, "registry-push-creds volume mount should be present")
	assert.Equal(t, "/docker", mount.MountPath)
	assert.True(t, mount.ReadOnly)

	// DOCKER_CONFIG env should point to the mount path
	dockerConfig := findEnv(job.Spec.Template.Spec.Containers[0].Env, "DOCKER_CONFIG")
	assert.Equal(t, "/docker", dockerConfig)
}

// TestBuildCopyJob_ImageRef verifies the image tag from resolvedRef is used as job name.
func TestBuildCopyJob_ImageRef(t *testing.T) {
	mc := minimalModelCache()
	mc.Status.ResolvedRef = "ghcr.io/jomcgi/models/llama:v1.0"
	cfg := minimalConfig()

	job := buildCopyJob(mc, cfg)

	// JobName extracts the tag from the ref
	assert.Equal(t, naming.JobName("ghcr.io/jomcgi/models/llama:v1.0"), job.Name)
}

// --- Helper functions ---

// assertConsecutive checks that flag and value appear consecutively in args.
func assertConsecutive(t *testing.T, args []string, flag, value string) {
	t.Helper()
	for i, a := range args {
		if a == flag && i+1 < len(args) && args[i+1] == value {
			return
		}
	}
	t.Errorf("expected %q %q to appear consecutively in args %v", flag, value, args)
}

func findEnv(envVars []corev1.EnvVar, name string) string {
	for _, e := range envVars {
		if e.Name == name {
			return e.Value
		}
	}
	return ""
}

func findEnvFromSecret(envVars []corev1.EnvVar, name string) *corev1.SecretKeySelector {
	for _, e := range envVars {
		if e.Name == name && e.ValueFrom != nil {
			return e.ValueFrom.SecretKeyRef
		}
	}
	return nil
}

func findVolume(volumes []corev1.Volume, name string) *corev1.Volume {
	for i := range volumes {
		if volumes[i].Name == name {
			return &volumes[i]
		}
	}
	return nil
}

func findMount(mounts []corev1.VolumeMount, name string) *corev1.VolumeMount {
	for i := range mounts {
		if mounts[i].Name == name {
			return &mounts[i]
		}
	}
	return nil
}

// --- isJobComplete, isJobFailed, jobFailureReason ---

func TestIsJobComplete(t *testing.T) {
	tests := []struct {
		name     string
		job      *batchv1.Job
		expected bool
	}{
		{
			name:     "no conditions returns false",
			job:      &batchv1.Job{},
			expected: false,
		},
		{
			name: "Complete condition with ConditionTrue returns true",
			job: &batchv1.Job{
				Status: batchv1.JobStatus{
					Conditions: []batchv1.JobCondition{
						{Type: batchv1.JobComplete, Status: corev1.ConditionTrue},
					},
				},
			},
			expected: true,
		},
		{
			name: "Complete condition with ConditionFalse returns false",
			job: &batchv1.Job{
				Status: batchv1.JobStatus{
					Conditions: []batchv1.JobCondition{
						{Type: batchv1.JobComplete, Status: corev1.ConditionFalse},
					},
				},
			},
			expected: false,
		},
		{
			name: "Failed condition does not satisfy isJobComplete",
			job: &batchv1.Job{
				Status: batchv1.JobStatus{
					Conditions: []batchv1.JobCondition{
						{Type: batchv1.JobFailed, Status: corev1.ConditionTrue},
					},
				},
			},
			expected: false,
		},
		{
			name: "multiple conditions — Complete True among them returns true",
			job: &batchv1.Job{
				Status: batchv1.JobStatus{
					Conditions: []batchv1.JobCondition{
						{Type: batchv1.JobSuspended, Status: corev1.ConditionFalse},
						{Type: batchv1.JobComplete, Status: corev1.ConditionTrue},
					},
				},
			},
			expected: true,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			assert.Equal(t, tc.expected, isJobComplete(tc.job))
		})
	}
}

func TestIsJobFailed(t *testing.T) {
	tests := []struct {
		name     string
		job      *batchv1.Job
		expected bool
	}{
		{
			name:     "no conditions returns false",
			job:      &batchv1.Job{},
			expected: false,
		},
		{
			name: "Failed condition with ConditionTrue returns true",
			job: &batchv1.Job{
				Status: batchv1.JobStatus{
					Conditions: []batchv1.JobCondition{
						{Type: batchv1.JobFailed, Status: corev1.ConditionTrue},
					},
				},
			},
			expected: true,
		},
		{
			name: "Failed condition with ConditionFalse returns false",
			job: &batchv1.Job{
				Status: batchv1.JobStatus{
					Conditions: []batchv1.JobCondition{
						{Type: batchv1.JobFailed, Status: corev1.ConditionFalse},
					},
				},
			},
			expected: false,
		},
		{
			name: "Complete condition does not satisfy isJobFailed",
			job: &batchv1.Job{
				Status: batchv1.JobStatus{
					Conditions: []batchv1.JobCondition{
						{Type: batchv1.JobComplete, Status: corev1.ConditionTrue},
					},
				},
			},
			expected: false,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			assert.Equal(t, tc.expected, isJobFailed(tc.job))
		})
	}
}

func TestJobFailureReason(t *testing.T) {
	t.Run("returns message from Failed condition with ConditionTrue", func(t *testing.T) {
		job := &batchv1.Job{
			Status: batchv1.JobStatus{
				Conditions: []batchv1.JobCondition{
					{
						Type:    batchv1.JobFailed,
						Status:  corev1.ConditionTrue,
						Message: "BackoffLimitExceeded",
					},
				},
			},
		}
		assert.Equal(t, "BackoffLimitExceeded", jobFailureReason(job))
	})

	t.Run("returns unknown failure when no conditions", func(t *testing.T) {
		assert.Equal(t, "unknown failure", jobFailureReason(&batchv1.Job{}))
	})

	t.Run("returns unknown failure when Failed condition is False", func(t *testing.T) {
		job := &batchv1.Job{
			Status: batchv1.JobStatus{
				Conditions: []batchv1.JobCondition{
					{
						Type:    batchv1.JobFailed,
						Status:  corev1.ConditionFalse,
						Message: "transient failure",
					},
				},
			},
		}
		assert.Equal(t, "unknown failure", jobFailureReason(job))
	})

	t.Run("returns unknown failure when only Complete condition present", func(t *testing.T) {
		job := &batchv1.Job{
			Status: batchv1.JobStatus{
				Conditions: []batchv1.JobCondition{
					{Type: batchv1.JobComplete, Status: corev1.ConditionTrue, Message: "done"},
				},
			},
		}
		assert.Equal(t, "unknown failure", jobFailureReason(job))
	})

	t.Run("returns empty message when Failed condition has no message", func(t *testing.T) {
		job := &batchv1.Job{
			Status: batchv1.JobStatus{
				Conditions: []batchv1.JobCondition{
					{Type: batchv1.JobFailed, Status: corev1.ConditionTrue},
				},
			},
		}
		// The function returns c.Message, which is empty string here.
		assert.Equal(t, "", jobFailureReason(job))
	})
}
