/* =====================================================================
 * main_additions.c
 *
 * Drop these pieces into the CubeMX-generated main.c, in the matching
 * USER CODE sections. This is NOT a standalone file — it assumes
 * hadc1, htim2, hdma_adc1 handles already exist from CubeMX init, and
 * that USB_DEVICE/CDC middleware (CDC_Transmit_FS, CDC_Receive_FS) is
 * already generated.
 * ===================================================================== */

/* ---- USER CODE BEGIN Includes ---- */
#include <string.h>
#include <stdint.h>
#include "usbd_cdc_if.h"
/* ---- USER CODE END Includes ---- */

/* ---- USER CODE BEGIN PD (private defines) ---- */
#define SAMPLES_PER_HALF   256U     /* pairs per half-buffer; tune for latency vs USB packet efficiency */
#define DMA_BUF_LEN        (SAMPLES_PER_HALF * 2U * 2U) /* *2 channels *2 halves */
#define APB1_TIMER_CLK_HZ  96000000UL /* TIM2 clock on this part (APB1 x2 since APB1 prescaler > 1) */
#define TX_FRAME_MAX       (2U + 1U + 2U + SAMPLES_PER_HALF*4U + 1U)
/* ---- USER CODE END PD ---- */

/* ---- USER CODE BEGIN PV (private variables) ---- */
static volatile uint16_t adc_dma_buf[DMA_BUF_LEN];   /* raw circular DMA target: ch0,ch1,ch0,ch1,... */
static uint8_t  tx_frame[TX_FRAME_MAX];
static volatile uint8_t  streaming = 0;
static volatile uint8_t  half_ready = 0;   /* 1 = first half ready, 2 = second half ready */
static uint8_t  seq_num = 0;
/* ---- USER CODE END PV ---- */

/* ---- USER CODE BEGIN PFP (private function prototypes) ---- */
static void Scope_Init(void);
static void Scope_ProcessHalf(uint8_t half_index);
static void Scope_SetSampleRate(uint16_t arr_ticks);
void Scope_OnCdcReceive(uint8_t *buf, uint32_t len); /* called from CDC_Receive_FS override */
/* ---- USER CODE END PFP ---- */

/* ---- USER CODE BEGIN 2 (end of main() init, before while(1)) ---- */
Scope_Init();
/* ---- USER CODE END 2 ---- */

/* ---- USER CODE BEGIN WHILE (inside while(1)) ---- */
if (half_ready) {
    uint8_t idx = half_ready;
    half_ready = 0;
    Scope_ProcessHalf(idx);
}
/* ---- USER CODE END WHILE ---- */


/* ---- USER CODE BEGIN 4 (function bodies) ---- */

static void Scope_SetSampleRate(uint16_t arr_ticks)
{
    /* Stop timer, reload, restart -- avoids a torn/partial period */
    HAL_TIM_Base_Stop(&htim2);
    __HAL_TIM_SET_AUTORELOAD(&htim2, arr_ticks);
    __HAL_TIM_SET_COUNTER(&htim2, 0);
    HAL_TIM_Base_Start(&htim2);
}

static void Scope_Init(void)
{
    /* Default: conservative aggregate rate safe for CDC-over-FullSpeed.
     * 96 MHz / 480 = 200 kHz TIM2 update rate = 200k sample-PAIRS/sec
     * = 200 kS/s per channel aggregate throughput needed ~ 800 kB/s raw,
     * which is close to the practical CDC ceiling -- start here and let
     * the PC app back this off via the 'R' command if it sees dropped
     * frames, or push it higher if your USB link handles it cleanly. */
    Scope_SetSampleRate((uint16_t)(APB1_TIMER_CLK_HZ / 200000UL));

    memset((void *)adc_dma_buf, 0, sizeof(adc_dma_buf));
    streaming = 0;
    half_ready = 0;
    seq_num = 0;

    /* Start ADC in DMA circular mode now; we just don't act on the
     * interrupt callbacks below until 'streaming' is set, so the very
     * first 'S' command doesn't have to pay ADC startup latency. */
    HAL_ADC_Start_DMA(&hadc1, (uint32_t *)adc_dma_buf, DMA_BUF_LEN);
}

/* HAL DMA half-transfer / full-transfer callbacks -- these fire from
 * interrupt context, so keep them to setting a flag only. */
void HAL_ADC_ConvHalfCpltCallback(ADC_HandleTypeDef *hadc)
{
    if (streaming) half_ready = 1;
}

void HAL_ADC_ConvCpltCallback(ADC_HandleTypeDef *hadc)
{
    if (streaming) half_ready = 2;
}

static void Scope_ProcessHalf(uint8_t half_index)
{
    const uint16_t *src = (half_index == 1)
        ? (const uint16_t *)&adc_dma_buf[0]
        : (const uint16_t *)&adc_dma_buf[SAMPLES_PER_HALF * 2U];

    uint32_t pos = 0;
    tx_frame[pos++] = 0xAA;
    tx_frame[pos++] = 0x55;
    tx_frame[pos++] = seq_num++;
    tx_frame[pos++] = (uint8_t)(SAMPLES_PER_HALF & 0xFF);
    tx_frame[pos++] = (uint8_t)((SAMPLES_PER_HALF >> 8) & 0xFF);

    uint8_t checksum = 0;
    for (uint32_t i = 0; i < SAMPLES_PER_HALF * 2U; i++) {
        uint16_t v = src[i];       /* alternating ch0,ch1 as produced by ADC scan */
        uint8_t lo = (uint8_t)(v & 0xFF);
        uint8_t hi = (uint8_t)((v >> 8) & 0xFF);
        tx_frame[pos++] = lo;
        tx_frame[pos++] = hi;
        checksum ^= lo;
        checksum ^= hi;
    }
    tx_frame[pos++] = checksum;

    /* CDC_Transmit_FS returns USBD_BUSY if the previous transfer hasn't
     * flushed yet -- at high rates this WILL happen occasionally; we
     * deliberately drop this frame rather than block, since blocking
     * here would desync the ADC/DMA timing. The PC side's sequence-number
     * check is what surfaces this as a "dropped frames" counter. */
    CDC_Transmit_FS(tx_frame, pos);
}

/* Call this from your CDC_Receive_FS() override in usbd_cdc_if.c,
 * passing through the buffer/length it receives from the host. */
void Scope_OnCdcReceive(uint8_t *buf, uint32_t len)
{
    if (len < 1) return;

    switch (buf[0]) {
        case 'S':
            seq_num = 0;
            streaming = 1;
            break;

        case 'X':
            streaming = 0;
            half_ready = 0;
            break;

        case 'R':
            if (len >= 3) {
                uint16_t ticks = (uint16_t)(buf[1] | (buf[2] << 8));
                Scope_SetSampleRate(ticks);
                uint8_t ack[3] = { 'r', buf[1], buf[2] };
                CDC_Transmit_FS(ack, sizeof(ack));
            }
            break;

        default:
            break;
    }
}
/* ---- USER CODE END 4 ---- */
