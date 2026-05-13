/*
 * RSRP Monitor
 *
 * Listens on a specified center frequency and bandwidth,
 * calculates and prints average RSRP every second.
 * Logs measurements to InfluxDB (uses server-side timestamps).
 *
 * Build: gcc -o rsrp_monitor rsrp_monitor.c -luhd -lm -lpthread -lcurl
 * Usage: ./rsrp_monitor -a "addr=192.168.10.2" -f 3600 -b 20 -g 30 -s "B:1"
 *        (frequency and bandwidth in MHz)
 */

#include <uhd.h>
#include <curl/curl.h>

#include <getopt.h>
#include <math.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <stdbool.h>

#define EXECUTE_OR_GOTO(label, ...) \
    if (__VA_ARGS__) {              \
        return_code = EXIT_FAILURE; \
        goto label;                 \
    }

/* ============ InfluxDB Configuration ============ */
#define INFLUX_URL "http://YOUR_INFLUXDB_URL:8086/api/v2/write?org=YOUR_ORG&bucket=YOUR_BUCKET"
#define INFLUX_TOKEN "YOUR_INFLUXDB_TOKEN"

/* Global state */
static volatile bool stop_signal_called = false;
static CURL* g_curl = NULL;
static struct curl_slist* g_headers = NULL;

void print_help(void)
{
    fprintf(stderr,
        "rsrp_monitor - Measure and report average RSRP with InfluxDB logging\n\n"
        "Options:\n"
        "    -a (device args)     USRP device arguments\n"
        "    -f (freq in MHz)     Center frequency in MHz [default: 3600]\n"
        "    -b (bw in MHz)       Bandwidth in MHz [default: 20]\n"
        "    -g (gain)            RX gain in dB [default: 30]\n"
        "    -c (channel)         RX channel index [default: 0]\n"
        "    -s (subdev)          RX subdevice spec (e.g., \"B:1\") [default: none]\n"
        "    -v                   Enable verbose output\n"
        "    -h                   Print this help message\n"
        "\n"
        "Subdevice Examples (X410):\n"
        "    A:0  - DB0, RF0 (channel 0)\n"
        "    A:1  - DB0, RF1 (channel 1)\n"
        "    B:0  - DB1, RF0 (channel 2)\n"
        "    B:1  - DB1, RF1 (channel 3)\n"
        "\n"
        "Usage Examples:\n"
        "    # Monitor 3.6 GHz with 20 MHz bandwidth\n"
        "    ./rsrp_monitor -a \"addr=192.168.10.2\" -f 3600 -b 20 -g 30\n"
        "\n"
        "    # Monitor 3.8175 GHz with 5 MHz bandwidth (3.815-3.820 GHz)\n"
        "    ./rsrp_monitor -a \"addr=192.168.10.2\" -f 3817.5 -b 5 -g 30 -s \"B:1\"\n");
}

void sigint_handler(int code)
{
    (void)code;
    stop_signal_called = true;
}

size_t discard_response_cb(char* contents, size_t size, size_t nmemb, void* userp)
{
    (void)contents;
    (void)userp;
    return size * nmemb;
}

int init_influx(void)
{
    curl_global_init(CURL_GLOBAL_DEFAULT);
    g_curl = curl_easy_init();

    if (!g_curl) {
        fprintf(stderr, "[InfluxDB] Failed to initialize curl\n");
        return -1;
    }

    char auth_header[256];
    snprintf(auth_header, sizeof(auth_header), "Authorization: Token %s", INFLUX_TOKEN);
    g_headers = curl_slist_append(g_headers, auth_header);
    g_headers = curl_slist_append(g_headers, "Content-Type: text/plain");

    curl_easy_setopt(g_curl, CURLOPT_URL, INFLUX_URL);
    curl_easy_setopt(g_curl, CURLOPT_HTTPHEADER, g_headers);
    curl_easy_setopt(g_curl, CURLOPT_WRITEFUNCTION, discard_response_cb);
    curl_easy_setopt(g_curl, CURLOPT_SSL_VERIFYPEER, 0L);

    return 0;
}

void cleanup_influx(void)
{
    if (g_headers) {
        curl_slist_free_all(g_headers);
        g_headers = NULL;
    }
    if (g_curl) {
        curl_easy_cleanup(g_curl);
        g_curl = NULL;
    }
    curl_global_cleanup();
}

int log_to_influx(const char* measurement, const char* tags, const char* fields)
{
    if (!g_curl) return -1;

    char line[1024];
    /* No timestamp - let InfluxDB use server time for easy correlation */
    snprintf(line, sizeof(line), "%s,%s %s", measurement, tags, fields);

    curl_easy_setopt(g_curl, CURLOPT_POSTFIELDS, line);

    CURLcode res = curl_easy_perform(g_curl);
    if (res != CURLE_OK) {
        fprintf(stderr, "[InfluxDB] curl_easy_perform() failed: %s\n", curl_easy_strerror(res));
        return -1;
    }

    return 0;
}

void log_measurement(double power_dbm, double freq_mhz, double bw_mhz)
{
    char tags[256];
    char fields[512];

    double band_start_mhz = freq_mhz - bw_mhz / 2.0;
    double band_stop_mhz = freq_mhz + bw_mhz / 2.0;

    snprintf(tags, sizeof(tags), "source=rsrp_monitor");
    snprintf(fields, sizeof(fields),
             "power=%.2f,center_freq=%.2f,bandwidth=%.2f,band_start=%.2f,band_stop=%.2f",
             power_dbm, freq_mhz, bw_mhz, band_start_mhz, band_stop_mhz);

    log_to_influx("measured_interference", tags, fields);
}

/*
 * Calculate average power in dBm from IQ samples.
 */
double calculate_avg_power_dbm(const float* samples, size_t num_samples, double gain_db)
{
    if (num_samples == 0) return -INFINITY;

    double sum_power = 0.0;

    for (size_t i = 0; i < num_samples * 2; i += 2) {
        float re = samples[i];
        float im = samples[i + 1];
        sum_power += (re * re + im * im);
    }

    double avg_power = sum_power / num_samples;

    double power_dbfs = 10.0 * log10(avg_power + 1e-20);
    double power_dbm = power_dbfs - gain_db;

    return power_dbm;
}

int main(int argc, char* argv[])
{
    int option           = 0;
    double freq_mhz      = 3600.0;
    double bw_mhz        = 20.0;
    double gain          = 30.0;
    char* device_args    = NULL;
    char* subdev_spec    = NULL;
    size_t channel       = 0;
    bool verbose         = false;
    int return_code      = EXIT_SUCCESS;
    char error_string[512];

    /* Process options */
    while ((option = getopt(argc, argv, "a:f:b:g:c:s:vh")) != -1) {
        switch (option) {
            case 'a':
                device_args = strdup(optarg);
                break;
            case 'f':
                freq_mhz = atof(optarg);
                break;
            case 'b':
                bw_mhz = atof(optarg);
                break;
            case 'g':
                gain = atof(optarg);
                break;
            case 'c':
                channel = (size_t)atoi(optarg);
                break;
            case 's':
                subdev_spec = strdup(optarg);
                break;
            case 'v':
                verbose = true;
                break;
            case 'h':
                print_help();
                goto free_option_strings;
            default:
                print_help();
                return_code = EXIT_FAILURE;
                goto free_option_strings;
        }
    }

    if (!device_args)
        device_args = strdup("");

    /* Convert to Hz */
    double freq_hz = freq_mhz * 1e6;
    double rate_hz = bw_mhz * 1e6;

    fprintf(stderr, "RSRP Monitor Configuration:\n");
    fprintf(stderr, "  Center Frequency: %.2f MHz\n", freq_mhz);
    fprintf(stderr, "  Bandwidth:        %.2f MHz\n", bw_mhz);
    fprintf(stderr, "  RX Gain:          %.1f dB\n", gain);
    if (subdev_spec) {
        fprintf(stderr, "  Subdev Spec:      %s\n", subdev_spec);
    } else {
        fprintf(stderr, "  Channel:          %zu\n", channel);
    }
    fprintf(stderr, "\n");

    /* Initialize InfluxDB */
    if (init_influx() != 0) {
        fprintf(stderr, "Warning: InfluxDB initialization failed, continuing without logging\n");
    }

    /* Create USRP */
    uhd_usrp_handle usrp;
    fprintf(stderr, "Creating USRP with args \"%s\"...\n", device_args);
    EXECUTE_OR_GOTO(free_curl, uhd_usrp_make(&usrp, device_args))

    /* Set subdevice spec if specified */
    if (subdev_spec) {
        uhd_subdev_spec_handle spec;
        EXECUTE_OR_GOTO(free_usrp, uhd_subdev_spec_make(&spec, subdev_spec))

        fprintf(stderr, "Setting RX subdev spec: %s\n", subdev_spec);
        uhd_error err = uhd_usrp_set_rx_subdev_spec(usrp, spec, 0);
        uhd_subdev_spec_free(&spec);

        if (err != UHD_ERROR_NONE) {
            fprintf(stderr, "Failed to set RX subdev spec\n");
            return_code = EXIT_FAILURE;
            goto free_usrp;
        }

        channel = 0;
    }

    /* Print the actual subdev being used */
    {
        uhd_subdev_spec_handle current_spec;
        uhd_subdev_spec_make(&current_spec, "");
        uhd_usrp_get_rx_subdev_spec(usrp, 0, current_spec);

        char spec_str[256];
        uhd_subdev_spec_to_string(current_spec, spec_str, sizeof(spec_str));
        fprintf(stderr, "Active RX subdev spec: %s\n", spec_str);

        uhd_subdev_spec_free(&current_spec);
    }

    /* Create RX streamer */
    uhd_rx_streamer_handle rx_streamer;
    EXECUTE_OR_GOTO(free_usrp, uhd_rx_streamer_make(&rx_streamer))

    /* Create RX metadata */
    uhd_rx_metadata_handle md;
    EXECUTE_OR_GOTO(free_rx_streamer, uhd_rx_metadata_make(&md))

    /* Set up tune request */
    uhd_tune_request_t tune_request = {
        .target_freq = freq_hz,
        .rf_freq_policy = UHD_TUNE_REQUEST_POLICY_AUTO,
        .dsp_freq_policy = UHD_TUNE_REQUEST_POLICY_AUTO
    };
    uhd_tune_result_t tune_result;

    uhd_stream_args_t stream_args = {
        .cpu_format = "fc32",
        .otw_format = "sc16",
        .args = "",
        .channel_list = &channel,
        .n_channels = 1
    };

    uhd_stream_cmd_t stream_cmd = {
        .stream_mode = UHD_STREAM_MODE_START_CONTINUOUS,
        .num_samps = 0,
        .stream_now = true,
        .time_spec_full_secs = 0,
        .time_spec_frac_secs = 0
    };

    size_t samps_per_buff;
    float* buff = NULL;
    void** buffs_ptr = NULL;

    /* Set sample rate */
    fprintf(stderr, "Setting RX Rate: %.2f MHz...\n", rate_hz / 1e6);
    EXECUTE_OR_GOTO(free_rx_metadata, uhd_usrp_set_rx_rate(usrp, rate_hz, channel))
    EXECUTE_OR_GOTO(free_rx_metadata, uhd_usrp_get_rx_rate(usrp, channel, &rate_hz))
    fprintf(stderr, "Actual RX Rate: %.2f MHz\n", rate_hz / 1e6);

    /* Set gain */
    fprintf(stderr, "Setting RX Gain: %.1f dB...\n", gain);
    EXECUTE_OR_GOTO(free_rx_metadata, uhd_usrp_set_rx_gain(usrp, gain, channel, ""))
    EXECUTE_OR_GOTO(free_rx_metadata, uhd_usrp_get_rx_gain(usrp, channel, "", &gain))
    fprintf(stderr, "Actual RX Gain: %.1f dB\n", gain);

    /* Set frequency */
    fprintf(stderr, "Setting RX frequency: %.2f MHz...\n", freq_hz / 1e6);
    EXECUTE_OR_GOTO(free_rx_metadata, uhd_usrp_set_rx_freq(usrp, &tune_request, channel, &tune_result))
    EXECUTE_OR_GOTO(free_rx_metadata, uhd_usrp_get_rx_freq(usrp, channel, &freq_hz))
    fprintf(stderr, "Actual RX frequency: %.2f MHz\n", freq_hz / 1e6);

    /* Update freq_mhz with actual value */
    freq_mhz = freq_hz / 1e6;
    bw_mhz = rate_hz / 1e6;

    /* Set up streamer */
    EXECUTE_OR_GOTO(free_rx_metadata, uhd_usrp_get_rx_stream(usrp, &stream_args, rx_streamer))

    /* Get buffer size */
    EXECUTE_OR_GOTO(free_rx_metadata, uhd_rx_streamer_max_num_samps(rx_streamer, &samps_per_buff))
    fprintf(stderr, "Buffer size: %zu samples\n", samps_per_buff);

    /* Allocate buffer */
    buff = calloc(samps_per_buff * 2, sizeof(float));
    if (!buff) {
        fprintf(stderr, "Failed to allocate buffer\n");
        return_code = EXIT_FAILURE;
        goto free_rx_metadata;
    }
    buffs_ptr = (void**)&buff;

    /* Start streaming */
    fprintf(stderr, "\nStarting RX stream...\n");
    EXECUTE_OR_GOTO(free_buff, uhd_rx_streamer_issue_stream_cmd(rx_streamer, &stream_cmd))

    /* Set up signal handler */
    signal(SIGINT, &sigint_handler);
    fprintf(stderr, "Press Ctrl+C to stop...\n\n");

    /* Print header */
    printf("Timestamp,Avg_Power_dBm\n");
    fflush(stdout);

    /* Main receive loop - report every 100ms */
    const double REPORT_INTERVAL_SEC = 0.1;  /* 100ms */
    size_t samples_accumulated = 0;
    double power_accumulator = 0.0;
    size_t measurement_count = 0;

    struct timespec last_report_ts;
    clock_gettime(CLOCK_MONOTONIC, &last_report_ts);

    while (!stop_signal_called) {
        size_t num_rx_samps = 0;

        /* Receive samples */
        uhd_rx_streamer_recv(rx_streamer, buffs_ptr, samps_per_buff, &md, 1.0, false, &num_rx_samps);

        if (num_rx_samps == 0) {
            continue;
        }

        /* Check for errors */
        uhd_rx_metadata_error_code_t error_code;
        uhd_rx_metadata_error_code(md, &error_code);

        if (error_code == UHD_RX_METADATA_ERROR_CODE_OVERFLOW) {
            if (verbose) {
                fprintf(stderr, "Overflow detected\n");
            }
            continue;
        } else if (error_code != UHD_RX_METADATA_ERROR_CODE_NONE) {
            if (verbose) {
                fprintf(stderr, "RX error code: %d\n", error_code);
            }
            continue;
        }

        /* Calculate power for this buffer */
        double buffer_power_dbm = calculate_avg_power_dbm(buff, num_rx_samps, gain);

        /* Accumulate (in linear scale for proper averaging) */
        power_accumulator += pow(10.0, buffer_power_dbm / 10.0);
        measurement_count++;
        samples_accumulated += num_rx_samps;

        /* Check if 100ms has elapsed */
        struct timespec now_ts;
        clock_gettime(CLOCK_MONOTONIC, &now_ts);
        double elapsed = (now_ts.tv_sec - last_report_ts.tv_sec) +
                         (now_ts.tv_nsec - last_report_ts.tv_nsec) / 1e9;

        if (elapsed >= REPORT_INTERVAL_SEC && measurement_count > 0) {
            /* Calculate average power */
            double avg_power_linear = power_accumulator / measurement_count;
            double avg_power_dbm = 10.0 * log10(avg_power_linear);

            /* Get wall-clock timestamp for display */
            struct timespec wall_ts;
            clock_gettime(CLOCK_REALTIME, &wall_ts);
            struct tm* tm_info = localtime(&wall_ts.tv_sec);
            char time_str[64];
            strftime(time_str, sizeof(time_str), "%Y-%m-%d %H:%M:%S", tm_info);
            int ms = wall_ts.tv_nsec / 1000000;

            /* Print result with milliseconds */
            printf("%s.%03d,%.2f\n", time_str, ms, avg_power_dbm);
            fflush(stdout);

            /* Log to InfluxDB */
            log_measurement(avg_power_dbm, freq_mhz, bw_mhz);

            if (verbose) {
                fprintf(stderr, "[%s.%03d] Samples: %zu, Measurements: %zu, Avg Power: %.2f dBm\n",
                        time_str, ms, samples_accumulated, measurement_count, avg_power_dbm);
            }

            /* Reset accumulators */
            power_accumulator = 0.0;
            measurement_count = 0;
            samples_accumulated = 0;
            last_report_ts = now_ts;
        }
    }

    /* Stop streaming */
    stream_cmd.stream_mode = UHD_STREAM_MODE_STOP_CONTINUOUS;
    uhd_rx_streamer_issue_stream_cmd(rx_streamer, &stream_cmd);

    fprintf(stderr, "\nStopping...\n");

free_buff:
    free(buff);

free_rx_metadata:
    if (verbose) fprintf(stderr, "Cleaning up RX metadata.\n");
    uhd_rx_metadata_free(&md);

free_rx_streamer:
    if (verbose) fprintf(stderr, "Cleaning up RX streamer.\n");
    uhd_rx_streamer_free(&rx_streamer);

free_usrp:
    if (verbose) fprintf(stderr, "Cleaning up USRP.\n");
    if (return_code != EXIT_SUCCESS && usrp != NULL) {
        uhd_usrp_last_error(usrp, error_string, 512);
        fprintf(stderr, "USRP reported the following error: %s\n", error_string);
    }
    uhd_usrp_free(&usrp);

free_curl:
    cleanup_influx();

free_option_strings:
    if (device_args) free(device_args);
    if (subdev_spec) free(subdev_spec);

    fprintf(stderr, (return_code ? "Failure\n" : "Done\n"));
    return return_code;
}
