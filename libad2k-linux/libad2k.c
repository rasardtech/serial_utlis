
#if HAVE_CONFIG_H
#include "config.h"
#endif

#include <string.h>
#include <stdio.h>
#include <unistd.h>
#include <stdlib.h>
#include <errno.h>
#include <fcntl.h>
#include <termios.h>
#include <pthread.h>
#include <link.h>
#include <sys/types.h>
#include <sys/stat.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/ipc.h>
#include <sys/msg.h>

#include "libad2k.h"


#define SOH  0x01
#define STX  0x02
#define ETX  0x03
#define EOT  0x04
#define ENQ  0x05
#define ACK  0x06
#define BEL  0x07
#define BS   0x08
#define HT   0x09
#define LF   0x0a
#define VT   0x0b
#define FF   0x0c
#define CRT  0x0d
#define SO   0x0e
#define SI   0x0f
#define DLE  0x10
#define DC1  0x11
#define DC2  0x12
#define DC3  0x13
#define DC4  0x14
#define NAK  0x15
#define SYN  0x16
#define ETB  0x17
#define CAN  0x18
#define EM   0x19
#define SUB  0x1a
#define ESC  0x1b
#define FS   0x1c
#define GS   0x1d
#define RS   0x1e
#define US   0x1f

#define XON  DC1
#define XOFF DC3


#define TIME_OUT_SPEC_mSEC  2000
#define TIME_OUT_SPAN_mSEC  20000
#define TIME_OUT_CMD_mSEC   500


#define MSGQ_FR_AD_KEY 0x22000
#define MSGQ_TO_AD_KEY 0x22001


struct sndCmd {
	unsigned char cmd;
	unsigned char data[1024];
	int data_len;
	int timeout;
};

struct NIData {
	unsigned char mode; //'N': weight mode, 'I': internal mode
	unsigned char sec1Str[16];
	unsigned char sec2Str[16];
	int sec1;
	int sec2;
	unsigned char status[NBYTE_NI_STATUS];
};


static unsigned char cmd_saved = 0;
static volatile int waitRespFg = 0;
//static volatile struct Response *gResp = NULL;


static int scale_fd = -1;
static pthread_t thread_fr_ad_id = 0;
static pthread_t thread_to_ad_id = 0;

static int msgq_fr_ad_id = -1;
static int msgq_to_ad_id = -1;


static int openPort(struct ad2k_port *portConf);
static void *scaleProcedureFrAD(void *lpParam);
static void *scaleProcedureToAD(void *lpParam);
static int fetchContent(int fd, unsigned char *content, int maxSize);
static void getResponse(struct Response *resp, const unsigned char *content, int n);
static void getNIData(struct NIData *NIdata, const unsigned char *content);
static void processResponse(int msgq_id, const struct Response *resp);
static void processNIData(int msgq_id, const struct NIData *NIdata);
static void NIDataToScaleData(struct ScaleData *scaleData, const struct NIData *NIdata);
static void respDataToADVerStr(char *adVer, const unsigned char *respData);
static void respDataToSpanData(struct SpanData *spanData, const unsigned char *respData);
static void respDataToSpecData(struct SpecData *specData, const unsigned char *respData);
static void specDataToCmdData(unsigned char *cmdData, const struct SpecData *specData);
static int xonXoffCmd(unsigned char xon_xoff);
static int sendRCmd(unsigned char rd_cmd, int timeout);
static int sendWCmd(unsigned char wr_cmd, unsigned char *data, int data_len, int timeout);
static int sendCmd(unsigned char type, unsigned char cmd, unsigned char *data, int data_len, int timeout);
static int sendXCmd(unsigned char xon_xoff);
static unsigned char calculateBCC(unsigned char *data, int len);
static void cutHeadZero(char *dst, char *src);
static void cutTailZero(char *dst, char *src);
static void msleep(int msec);

static int msgq_create(int key);
static int msgq_remove(int msgq_id);
static int msgq_recv_msg(int msgq_id, void *buf);
static int msgq_send_msg(int msgq_id, int msg_type, void *buf, int len);
static int msgq_rcv(int msgq_id, struct ad2k_msg_s *msg);
static int msgq_snd(int msgq_id, struct ad2k_msg_s *msg, int n);
static void msgq_clear(int msgq_id);

static unsigned int crc32_table[256];
static void init_crc32_table();
static unsigned int crc32(unsigned char *buf, int len);
static unsigned int calculateChecksum(char *fileName);


int ad2k_OpenScale(struct ad2k_port *portConf)
{
	if (scale_fd > 0) {
		return 0;
	}

	if (portConf == NULL) {
		return -1;
	}

	scale_fd = openPort(portConf);
	if (scale_fd < 0) {
		return -1;
	}

	return 0;
}

void ad2k_CloseScale(void)
{
	if (scale_fd < 0) {
		return;
	}

	close(scale_fd);
	scale_fd = -1;
}

int ad2k_StartScale(void)
{
	msgq_fr_ad_id = msgq_create(MSGQ_FR_AD_KEY);
	if (msgq_fr_ad_id < 0) {
		return -1;
	}
	msgq_clear(msgq_fr_ad_id);

	msgq_to_ad_id = msgq_create(MSGQ_TO_AD_KEY);
	if (msgq_to_ad_id < 0) {
		return -1;
	}
	msgq_clear(msgq_to_ad_id);

	xonXoffCmd(XON);

	if (thread_fr_ad_id == 0) {
		if (pthread_create(&thread_fr_ad_id, NULL, scaleProcedureFrAD, NULL) < 0) {
			return -1;
		}
	}
	if (thread_to_ad_id == 0) {
		if (pthread_create(&thread_to_ad_id, NULL, scaleProcedureToAD, NULL) < 0) {
			return -1;
		}
	}

	return 0;
}

void ad2k_StopScale(void)
{
	if (thread_fr_ad_id > 0) {
		pthread_cancel(thread_fr_ad_id);
		thread_fr_ad_id = 0;
	}
	if (thread_to_ad_id > 0) {
		pthread_cancel(thread_to_ad_id);
		thread_to_ad_id = 0;
	}

	xonXoffCmd(XOFF);

	if (msgq_fr_ad_id > 0) {
		msgq_remove(msgq_fr_ad_id);
		msgq_fr_ad_id = -1;
	}
	if (msgq_to_ad_id > 0) {
		msgq_remove(msgq_to_ad_id);
		msgq_to_ad_id = -1;
	}
}

int  ad2k_Xon(void)
{
	xonXoffCmd(XON);
}

int  ad2k_Xoff(void)
{
	return xonXoffCmd(XOFF);
}

int ad2k_FetchData(struct AD2kData *ad2kData)
{
	if (ad2kData == NULL) {
		return -1;
	}

	while (waitRespFg);
	return msgq_recv_msg(msgq_fr_ad_id, ad2kData);
}

/* 'R' command */

int ad2k_GetADVer(void)
{
	return sendRCmd(CMD_R_AD_VER, TIME_OUT_CMD_mSEC);
}

int ad2k_GetSpecData(void)
{
	return sendRCmd(CMD_RW_SPEC_DATA, TIME_OUT_CMD_mSEC);
}

int ad2k_GetSpanData(void)
{
	return sendRCmd(CMD_RW_SPAN_DATA, TIME_OUT_CMD_mSEC);
}

/* 'W' command */

int ad2k_SetSpecData(struct SpecData *scale_spec)
{
	unsigned char data[LEN_RW_SPEC_DATA];

	if (scale_spec == NULL) {
		return -1;
	}

	memset(data, 0, sizeof(data));
	specDataToCmdData(data, scale_spec);

	return sendWCmd(CMD_RW_SPEC_DATA, data, LEN_RW_SPEC_DATA, TIME_OUT_SPEC_mSEC);
}

int ad2k_SetSpanData(struct SpanData *span_data)
{
	unsigned char data[32];

	if (span_data == NULL) {
		return -1;
	}

	memset(data, 0, sizeof(data));
	snprintf((char *)data, sizeof(data),
		"%08d%08d%08d%02d%02d",
		span_data->firstWeight,
		span_data->spanWeight,
		span_data->secWeight,
		span_data->e2,
		span_data->e1);

	return sendWCmd(CMD_RW_SPAN_DATA, data, LEN_RW_SPAN_DATA, TIME_OUT_SPAN_mSEC);
}

int ad2k_SetChkSum(unsigned int checkSum)
{
	unsigned char data[16];

	memset(data, 0, sizeof(data));
	snprintf((char *) data, sizeof(data), "%08X", checkSum);
	
	return sendWCmd(CMD_W_SET_CHKSUM, data, LEN_W_CHKSUM, TIME_OUT_CMD_mSEC);
}

int ad2k_ValidateChkSum(unsigned int checkSum)
{
	unsigned char data[16];

	memset(data, 0, sizeof(data));
	snprintf((char *) data, sizeof(data), "%08X", checkSum);

	return sendWCmd(CMD_W_VLD_CHKSUM, data, LEN_W_CHKSUM, TIME_OUT_CMD_mSEC);
}

int ad2k_CalculateChkSum()
{
	return ad2k_CalculateFileChkSum(NULL);
}

int ad2k_CalculateFileChkSum(char *fileName)
{
	if (fileName == NULL || strlen(fileName) <= 0) {
		fileName = "/usr/local/lib/libad2k.so";
	}

	return calculateChecksum(fileName);
}

int ad2k_CalbSpan(void)
{
	return sendWCmd(CMD_W_CALB_SPAN, NULL, 0, TIME_OUT_CMD_mSEC);
}

int ad2k_CalbZero(void)
{
	return sendWCmd(CMD_W_CALB_ZERO, NULL, 0, TIME_OUT_CMD_mSEC);
}

int ad2k_CalbSpanG(void)
{	
	return sendWCmd(CMD_W_CALB_SPAN_G, NULL, 0, TIME_OUT_CMD_mSEC);
}

int ad2k_CorrectG(int gravity)
{
	unsigned char data[16];

	memset(data, 0, sizeof(data));
	snprintf((char *)data, sizeof(data), "%08d", gravity);

	return sendWCmd(CMD_W_CRCT_G, data, LEN_W_CRCT_G, TIME_OUT_CMD_mSEC);
}

int ad2k_SetOneTouchTare(void)
{
	return sendWCmd(CMD_W_OTCH_TARE, NULL, 0, TIME_OUT_CMD_mSEC);
}

int ad2k_SetDigitalTare(int tare)
{
	unsigned char data[16];

	memset(data, 0, sizeof(data));
	snprintf((char *)data, sizeof(data), "%08d", tare);

	return sendWCmd(CMD_W_DIGI_TARE, data, LEN_W_DIGI_TARE, TIME_OUT_CMD_mSEC);
}

int ad2k_SetPresetTare(int tare)
{
	unsigned char data[16];

	memset(data, 0, sizeof(data));
	snprintf((char *)data, sizeof(data), "%08d", tare);

	return sendWCmd(CMD_W_PRESET_TARE, data, LEN_W_PRESET_TARE, TIME_OUT_CMD_mSEC);
}

int ad2k_SetPercentTare(int tare)
{
	unsigned char data[16];

	memset(data, 0, sizeof(data));
	snprintf((char *)data, sizeof(data), "%04d", tare);

	return sendWCmd(CMD_W_PERCENT_TARE, data, LEN_W_PERCENT_TARE, TIME_OUT_CMD_mSEC);
}

int ad2k_ZeroReset(void)
{
	return sendWCmd(CMD_W_ZERO_RST, NULL, 0, TIME_OUT_CMD_mSEC);
}

int ad2k_ZeroResetPowerOn(void)
{
	return sendWCmd(CMD_W_ZRST_PWR_ON, NULL, 0, TIME_OUT_CMD_mSEC);
}

int ad2k_IRCntMode(void)
{
	return sendWCmd(CMD_W_INT_MODE, NULL, 0, TIME_OUT_CMD_mSEC);
}

int ad2k_WgtMode(void)
{
	return sendWCmd(CMD_W_WGT_MODE, NULL, 0, TIME_OUT_CMD_mSEC);
}

static void *scaleProcedureFrAD(void *lpParam)
{
	unsigned char content[256];
	int len;
	struct Response resp;
	struct NIData NIdata;

	while (1) {
		memset(content, 0, sizeof(content));
		len = fetchContent(scale_fd, content, sizeof(content));

		switch (len) {
		case 2:
		case 2+LEN_R_AD_VER:
		case 2+LEN_RW_SPEC_DATA:
		case 2+LEN_RW_SPAN_DATA:
			memset(&resp, 0, sizeof(resp));
			getResponse(&resp, content, len);
			processResponse(msgq_fr_ad_id, &resp);
			waitRespFg = 0;
			break;

		case LEN_NI_DATA:
			memset(&NIdata, 0, sizeof(NIdata));
			getNIData(&NIdata, content);
			processNIData(msgq_fr_ad_id, &NIdata);
			break;

		default:
			break;
		}
	}

	return 0;
}

static void *scaleProcedureToAD(void *lpParam)
{
	int n;
	char buf[256];

	while (1) {
		msleep(500);
		memset(buf, 0, sizeof(buf));
		n = msgq_recv_msg(msgq_to_ad_id, buf);
		if (n <= 0) {
			continue;
		}

		waitRespFg = 1;
		xonXoffCmd(XOFF);
		msleep(20);
		msgq_clear(msgq_fr_ad_id);
		write(scale_fd, buf, n);
		while (waitRespFg);
		xonXoffCmd(XON);
	}

	return 0;
}

static int fetchContent(int fd, unsigned char *content, int maxSize)
{
	int n;
	char ch;
	int i = 0;
	int stage = 0;

	if (fd < 0 || content == NULL || maxSize <= 0) {
		return -1;
	}

	while (1) {
		ch = 0;
		n = read(fd, &ch, 1);
		if (n <= 0) {
			continue;
		}

		if (i >= maxSize) {
			return maxSize;
		}

		switch (stage) {
		case 0:
			if (ch == STX) {
				stage++;
			}
			continue;

		case 1:
			if (ch == ETX) {
				stage++;
			} else {
				content[i++] = ch;
			}
			continue;

		case 2: //BCC
			if (calculateBCC(content, i) == ch) {
				return i;
			} else {
				return -1;
			}

		default:
			return -1;
		}
	}

	return -1;
}

static void getResponse(struct Response *resp, const unsigned char *content, int n)
{
	if (resp == NULL || content == NULL || n < 2) {
		return;
	}

	resp->cmd = cmd_saved;
	resp->type = content[0];
	resp->result = content[1];
	if (resp->type == CMD_R && resp->result == RSP_RW_OK) {
		memcpy(resp->data, &content[2], n - 2);
		resp->data_len = n - 2;
	} else {
		memset(resp->data, 0, sizeof(resp->data));
		resp->data_len = 0;
	}
}

static void getNIData(struct NIData *NIdata, const unsigned char *content)
{
	char buf[16];

	if (NIdata == NULL || content == NULL) {
		return;
	}

	if (content[0] == '0' && content[10] == '4') {  //weight mode
		NIdata->mode = CMD_W_WGT_MODE;
	} else if (content[0] == 'a' && content[10] == 'i') {  //internal mode
		NIdata->mode = CMD_W_INT_MODE;
	} else {
		return;
	}

	memcpy(buf, &content[1], 8);
	buf[8] = '\0';
	cutHeadZero((char *) NIdata->sec1Str, buf);
	NIdata->sec1 = ad2k_StrToNum((char *) NIdata->sec1Str, BASE_DEC);

	memcpy(buf, &content[11], 8);
	buf[8] = '\0';
	cutHeadZero((char *) NIdata->sec2Str, buf);
	NIdata->sec2 = ad2k_StrToNum((char *)NIdata->sec2Str, BASE_DEC);

	memset(NIdata->status, 0, sizeof(NIdata->status));
	ad2k_StrToHex(NIdata->status, (char *) &content[21],
		sizeof(NIdata->status) / sizeof(NIdata->status[0]));
}

static void processResponse(int msgq_id, const struct Response *resp)
{
	if (msgq_id < 0 || resp == NULL) {
		return;
	}

	static struct Response response;
	struct AD2kData ad2kData;
	//gResp = &response;

	memcpy(&response, resp, sizeof(struct Response));
	if (response.type == CMD_R && response.result == RSP_RW_OK) {
		if (response.cmd == CMD_RW_SPEC_DATA
			&& response.data_len == LEN_RW_SPEC_DATA) {

			ad2kData.type = AD2K_MSG_SPEC;
			respDataToSpecData(&ad2kData.data.scaleSpec, response.data);

		} else if (response.cmd == CMD_RW_SPAN_DATA
			&& response.data_len == LEN_RW_SPAN_DATA) {

			ad2kData.type = AD2K_MSG_SPAN;
			respDataToSpanData(&ad2kData.data.scaleSpan, response.data);

		} else if (response.cmd == CMD_R_AD_VER
			&& response.data_len == LEN_R_AD_VER) {

			ad2kData.type = AD2K_MSG_ADVER;
			respDataToADVerStr(ad2kData.data.adVer, response.data);
		}
	} else {
		//gResp = &response;
		ad2kData.type = AD2K_MSG_RESP;
		memcpy(&ad2kData.data.response, &response, sizeof(struct Response));
	}

	msgq_clear(msgq_id);
	msgq_send_msg(msgq_id, AD2K_MSG_DEFAULT, &ad2kData, sizeof(ad2kData));
}

static void processNIData(int msgq_id, const struct NIData *NIdata)
{
	if (NIdata == NULL) {
		return;
	}

	struct AD2kData ad2kData;
	
	ad2kData.type = AD2K_MSG_NI;
	NIDataToScaleData(&ad2kData.data.scaleData, NIdata);

	msgq_clear(msgq_id);
	msgq_send_msg(msgq_id, AD2K_MSG_DEFAULT, &ad2kData, sizeof(ad2kData));
}

static void NIDataToScaleData(struct ScaleData *scaleData, const struct NIData *NIdata)
{
	unsigned char status;

	if (scaleData == NULL || NIdata == NULL) {
		return;
	}

	status = NIdata->status[0];
	//byte 0, bit 0
	if (status & 0x01) {
		scaleData->tareFg = 1;
	} else {
		scaleData->tareFg = 0;
	}
	status >>= 3;

	//byte 0, bit 3
	if (status & 0x01) {
		scaleData->zeroPointFg = 1;
	} else {
		scaleData->zeroPointFg = 0;
	}
	status >>= 1;

	//byte 0, bit 4
	if (status & 0x01) {
		scaleData->stabilizeFg = 1;
	} else {
		scaleData->stabilizeFg = 0;
	}
	status >>= 2;

	/*
	//byte 0, bit 6
	if (status & 0x01) {
		scaleData->underflowFg = 1;
	} else {
		scaleData->underflowFg = 0;
	}
	status >>= 1;

	//byte 0, bit 7
	if (status & 0x01) {
		scaleData->overflowFg = 1;
	} else {
		scaleData->overflowFg = 0;
	}
	*/

	//byte 0, bit 6-7
	scaleData->underOverFlowFg = (status & 0x03) & 0xff;

	status = NIdata->status[1];
	status >>= 3;

	//byte 1, bit 3
	if (status & 0x01) {
		scaleData->spanSwFg = 1;
	} else {
		scaleData->spanSwFg = 0;
	}

	status = NIdata->status[2];

	//byte 2, bit 0
	if (status & 0x01) {
		scaleData->oneTouchTareFg = 1;
	} else {
		scaleData->oneTouchTareFg = 0;
	}
	status >>= 1;

	//byte 2, bit 1
	if (status & 0x01) {
		scaleData->digitalTareFg = 1;
	} else {
		scaleData->digitalTareFg = 0;
	}
	status >>= 1;

	//byte 2, bit 2
	if (status & 0x01) {
		scaleData->presetTareFg = 1;
	} else {
		scaleData->presetTareFg = 0;
	}
	status >>= 1;

	//byte 2, bit 3
	if (status & 0x01) {
		scaleData->percentTareFg = 1;
	} else {
		scaleData->percentTareFg = 0;
	}

	strncpy(scaleData->sec1Str, (char *) NIdata->sec1Str, sizeof(scaleData->sec1Str));
	strncpy(scaleData->sec2Str, (char *) NIdata->sec2Str, sizeof(scaleData->sec2Str));
	if (NIdata->mode == CMD_W_WGT_MODE) {
		if (scaleData->underOverFlowFg == 0) {
			scaleData->weight = NIdata->sec1;
		} else {
			scaleData->weight = 0;
		}
		scaleData->tare = NIdata->sec2;
		scaleData->irFg = 0;
	} else if (NIdata->mode == CMD_W_INT_MODE) {
		scaleData->adCount = NIdata->sec1;
		scaleData->irCount = NIdata->sec2;
		scaleData->irFg = 1;
	}
}

static void respDataToADVerStr(char *adVer, const unsigned char *respData)
{
	if (adVer == NULL || respData == NULL) {
		return;
	}

	snprintf(adVer, 8, "%c.%c.%c", respData[0], respData[1], respData[2]);
}

static void respDataToSpanData(struct SpanData *spanData, const unsigned char *respData)
{
	int i = 0;
	char buf[16];

	if (spanData == NULL || respData == NULL) {
		return;
	}

	memset(buf, 0, sizeof(buf));
	memcpy(buf, respData + i, 8);
	spanData->firstWeight = ad2k_StrToNum(buf, BASE_DEC);
	i += 8;

	memset(buf, 0, sizeof(buf));
	memcpy(buf, respData + i, 8);
	spanData->spanWeight = ad2k_StrToNum(buf, BASE_DEC);
	i += 8;

	memset(buf, 0, sizeof(buf));
	memcpy(buf, respData + i, 8);
	spanData->secWeight = ad2k_StrToNum(buf, BASE_DEC);
	i += 8;

	memset(buf, 0, sizeof(buf));
	memcpy(buf, respData + i, 2);
	spanData->e2 = ad2k_StrToNum(buf, BASE_DEC);
	i += 2;

	memset(buf, 0, sizeof(buf));
	memcpy(buf, respData + i, 2);
	spanData->e1 = ad2k_StrToNum(buf, BASE_DEC);
}

static void respDataToSpecData(struct SpecData *specData, const unsigned char *respData)
{
	unsigned char spec_byte[NBYTE_SPEC];

	if (specData == NULL || respData == NULL ) {
		return;
	}

	memset(spec_byte, 0, sizeof(spec_byte));
	ad2k_StrToHex(spec_byte, (char *) respData, sizeof(spec_byte) / sizeof(spec_byte[0]));

	//byte 0, bit 0
	if ((spec_byte[0] & 0x01) == 0) {
		specData->tareAutoClear = SV_ALLOW;
	} else {
		specData->tareAutoClear = SV_INHIBIT;
	}
	spec_byte[0] >>= 1;

	//byte 0, bit 1
	if ((spec_byte[0] & 0x01) == 0) {
		specData->autoZeroReset = SV_ALLOW;
	} else {
		specData->autoZeroReset = SV_INHIBIT;
	}
	spec_byte[0] >>= 1;

	//byte 0, bit 2-3
	switch (spec_byte[0] & 0x03) {
	case 0:
		specData->startRange = SV_SR_10;
		break;
	case 1:
		specData->startRange = SV_SR_20;
		break;
	case 2:
		specData->startRange = SV_SR_50;
		break;
	case 3:
	default:
		specData->startRange = SV_SR_100;
		break;
	}
	spec_byte[0] >>= 2;

	//byte 0, bit 4
	if ((spec_byte[0] & 0x01) == 0) {
		specData->tareSub = SV_ALLOW;
	} else {
		specData->tareSub = SV_INHIBIT;
	}
	spec_byte[0] >>= 1;

	//byte 0, bit 5
	if ((spec_byte[0] & 0x01) == 0) {
		specData->tareAcc = SV_ALLOW;
	} else {
		specData->tareAcc = SV_INHIBIT;
	}
	spec_byte[0] >>= 1;

	//byte 0, bit 6-7
	switch (spec_byte[0] & 0x03) {
	case 0:
		specData->wgtStabCond = SV_WSC_LOOSE;
		break;
	case 1:
		specData->wgtStabCond = SV_WSC_NORMAL;
		break;
	case 2:
		specData->wgtStabCond = SV_WSC_TIGHT;
		break;
	case 3:
		specData->wgtStabCond = SV_WSC_STRINGENT;
		break;
	default:
		specData->wgtStabCond = SV_WSC_NORMAL;
		break;
	}

	//byte 1, bit 0
	if ((spec_byte[1] & 0x01) == 0) {
		specData->zeroTrack = SV_ALLOW;
	} else {
		specData->zeroTrack = SV_INHIBIT;
	}
	spec_byte[1] >>= 1;

	//byte 1, bit 1
	if ((spec_byte[1] & 0x01) == 0) {
		specData->wgtReset = SV_ALLOW;
	} else {
		specData->wgtReset = SV_INHIBIT;
	}
	spec_byte[1] >>= 1;

	//byte 1, bit 2
	if ((spec_byte[1] & 0x01) == 0) {
		specData->digiTare = SV_ALLOW;
	} else {
		specData->digiTare = SV_INHIBIT;
	}
	spec_byte[1] >>= 1;

	//byte 1, bit 3
	if ((spec_byte[1] & 0x01) == 0) {
		specData->manTareCancel = SV_ALLOW;
	} else {
		specData->manTareCancel = SV_INHIBIT;
	}
	spec_byte[1] >>= 1;

	//byte 1, bit 4
	if ((spec_byte[1] & 0x01) == 0) {
		specData->zeroOn = SV_ZN_GROSS;
	} else {
		specData->zeroOn = SV_ZN_NET;
	}
	spec_byte[1] >>= 1;

	//byte 1, bit 5
	if ((spec_byte[1] & 0x01) == 0) {
		specData->tareAutoClear2 = SV_ALLOW;
	} else {
		specData->tareAutoClear2 = SV_INHIBIT;
	}
	spec_byte[1] >>= 1;

	//byte 1, bit 6
	if ((spec_byte[1] & 0x01) == 0) {
		specData->autoClearCond = SV_ACC_0;
	} else {
		specData->autoClearCond = SV_ACC_1;
	}
	spec_byte[1] >>= 1;

	//byte 1, bit 7
	if ((spec_byte[1] & 0x01) == 0) {
		specData->priTareOpe = SV_PTO_OT;
	} else {
		specData->priTareOpe = SV_PTO_DT;
	}

	//byte 2, bit 0
	if ((spec_byte[2] & 0x01) == 0) {
		specData->wtgSinMul1 = SV_WSM_SIG;
	} else {
		specData->wtgSinMul1 = SV_WSM_MUL;
	}
	spec_byte[2] >>= 1;

	//byte 2, bit 1
	if ((spec_byte[2] & 0x01) == 0) {
		specData->reZeroFunc = SV_ALLOW;
	} else {
		specData->reZeroFunc = SV_INHIBIT;
	}
	spec_byte[2] >>= 1;

	//byte 2, bit 2-3
	switch (spec_byte[2] & 0x03) {
	case 0:
		specData->reZeroRange = SV_RZR_2;
		break;
	case 1:
		specData->reZeroRange = SV_RZR_4;
		break;
	case 2:
		specData->reZeroRange = SV_RZR_10;
		break;
	case 3:
	default:
		specData->reZeroRange = SV_RZR_100;
		break;
	}
	spec_byte[2] >>= 2;

	//byte 2, bit 4, not used
	spec_byte[2] >>= 1;

	//byte 2, bit 5-7
	switch (spec_byte[2] & 0x07) {
	case 0:
		specData->posDecPoint1 = SV_DP_0;
		break;
	case 1:
		specData->posDecPoint1 = SV_DP_1;
		break;
	case 2:
		specData->posDecPoint1 = SV_DP_2;
		break;
	case 3:
		specData->posDecPoint1 = SV_DP_3;
		break;
	case 4:
		specData->posDecPoint1 = SV_DP_4;
		break;
	default:
		specData->posDecPoint1 = SV_DP_0;
		break;
	}

	//byte 3, bit 0
	/*
	if ((spec_byte[3] & 0x01) == 0) {
		specData->wtgSinMu2 = SV_WSM_SIG;
	} else {
		specData->wtgSinMu2 = SV_WSM_MUL;
	}
	*/
	spec_byte[3] >>= 1;

	//byte 3, bit 1-3
	/*
	switch (spec_byte[3] & 0x07) {
	case 0:
		specData->posDecPoint2 = SV_DP_0;
		break;
	case 1:
		specData->posDecPoint2 = SV_DP_1;
		break;
	case 2:
		specData->posDecPoint2 = SV_DP_2;
		break;
	case 3:
		specData->posDecPoint2 = SV_DP_3;
		break;
	case 4:
		specData->posDecPoint2 = SV_DP_4;
		break;
	default:
		specData->posDecPoint2 = SV_DP_0;
		break;
	}
	*/
	spec_byte[3] >>= 3;

	//byte 3, bit 4
	if ((spec_byte[3] & 0x01) == 0) {
		specData->decPointType = SV_TDP_P;
	} else {
		specData->decPointType = SV_TDP_C;
	}

	spec_byte[3] >>= 1;

	//byte 3, bit 5
	if ((spec_byte[3] & 0x01) == 0) {
		specData->tareRange = SV_TR_50;
	} else {
		specData->tareRange = SV_TR_100;
	}
	
	spec_byte[3] >>= 1;

	//byte3, bit 6-7
	switch (spec_byte[3] & 0x03) {
	case 0:
		specData->negWgtMsk = SV_NWM_MG9E; //Minus gross > 9e
		break;
	case 1:
		specData->negWgtMsk = SV_NWM_MGW;  //Minus gross Weight
		break;
	case 2:
		specData->negWgtMsk = SV_NWM_MGF;  //Minus gross > Full scale
		break;
	case 3:
	default:
		specData->negWgtMsk = SV_NWM_NU;    //not used
		break;
	}

	//byte4, bit 0-3, not used
	spec_byte[4] >>= 4;

	//byte4, bit 4-5
	/*
	switch (spec_byte[4] & 0x03) {
	case 0:
		specData->scale2FilterStrn = SV_FS_LO;
		break;
	case 1:
		specData->scale2FilterStrn = SV_FS_NM;
		break;
	case 2:
		specData->scale2FilterStrn = SV_FS_UN;
		break;
	case 3:
		specData->scale2FilterStrn = SV_FS_HI;
		break;
	default:
		specData->scale2FilterStrn = SV_FS_NM;
		break;
	}
	*/
	spec_byte[4] >>= 2;

	//byte4, bit 6-7
	switch (spec_byte[4] & 0x03) {
	case 0:
		specData->scale1FilterStrn = SV_FS_LO;
		break;
	case 1:
		specData->scale1FilterStrn = SV_FS_NM;
		break;
	case 2:
		specData->scale1FilterStrn = SV_FS_UN;
		break;
	case 3:
		specData->scale1FilterStrn = SV_FS_HI;
		break;
	default:
		specData->scale1FilterStrn = SV_FS_NM;
		break;
	}
}

static void specDataToCmdData(unsigned char *cmdData, const struct SpecData *specData)
{
	unsigned char spec_byte[NBYTE_SPEC];

	if (cmdData == NULL || specData == NULL) {
		return;
	}

	memset(spec_byte, 0, sizeof(spec_byte));

	//byte 0, bit 6-7
	switch (specData->wgtStabCond) {
	case SV_WSC_LOOSE:
		spec_byte[0] = 0x00;
		break;
	case SV_WSC_NORMAL:
		spec_byte[0] = 0x01;
		break;
	case SV_WSC_TIGHT:
		spec_byte[0] = 0x02;
		break;
	case SV_WSC_STRINGENT:
		spec_byte[0] = 0x03;
		break;
	default:
		spec_byte[0] = 0x01;
		break;
	}

	//byte 0, bit 5
	spec_byte[0] <<= 1;
	spec_byte[0] &= ~0x01; //1111 1110
	if (specData->tareAcc == SV_INHIBIT) {
		spec_byte[0] |= 0x01;
	}

	//byte 0, bit 4
	spec_byte[0] <<= 1;
	spec_byte[0] &= ~0x01; //1111 1110
	if (specData->tareSub == SV_INHIBIT) {
		spec_byte[0] |= 0x01;
	}

	//byte 0, bit 2-3
	spec_byte[0] <<= 2;
	spec_byte[0] &= ~0x03; //1111 1100
	switch (specData->startRange) {
	case SV_SR_10:
		break;
	case SV_SR_20:
		spec_byte[0] |= 0x01;
		break;
	case SV_SR_50:
		spec_byte[0] |= 0x02;
		break;
	case SV_SR_100:
	default:
		spec_byte[0] |= 0x03;
		break;
	}

	//byte 0, bit 1
	spec_byte[0] <<= 1;
	spec_byte[0] &= ~0x01; //1111 1110
	if (specData->autoZeroReset == SV_INHIBIT) {
		spec_byte[0] |= 0x01;
	}

	//byte 0, bit 0
	spec_byte[0] <<= 1;
	spec_byte[0] &= ~0x01; //1111 1110
	if (specData->tareAutoClear == SV_INHIBIT) {
		spec_byte[0] |= 0x01;
	}

	//byte 1, bit 7
	if (specData->priTareOpe == SV_PTO_OT) {
		spec_byte[1] = 0x00;
	} else {
		spec_byte[1] = 0x01;
	}

	//byte 1, bit 6
	spec_byte[1] <<= 1;
	spec_byte[1] &= ~0x01; //1111 1110
	if (specData->autoClearCond == SV_ACC_1) {
		spec_byte[1] |= 0x01;
	}

	//byte 1, bit 5
	spec_byte[1] <<= 1;
	spec_byte[1] &= ~0x01; //1111 1110
	if (specData->tareAutoClear2 == SV_INHIBIT) {
		spec_byte[1] |= 0x01;
	}

	//byte 1, bit 4
	spec_byte[1] <<= 1;
	spec_byte[1] &= ~0x01; //1111 1110
	if (specData->zeroOn == SV_ZN_NET) {
		spec_byte[1] |= 0x01;
	}

	//byte 1, bit 3
	spec_byte[1] <<= 1;
	spec_byte[1] &= ~0x01; //1111 1110
	if (specData->manTareCancel == SV_INHIBIT) {
		spec_byte[1] |= 0x01;
	}

	//byte 1, bit 2
	spec_byte[1] <<= 1;
	spec_byte[1] &= ~0x01; //1111 1110
	if (specData->digiTare == SV_INHIBIT) {
		spec_byte[1] |= 0x01;
	}

	//byte 1, bit 1
	spec_byte[1] <<= 1;
	spec_byte[1] &= ~0x01; //1111 1110
	if (specData->wgtReset == SV_INHIBIT) {
		spec_byte[1] |= 0x01;
	}

	//byte 1, bit 0
	spec_byte[1] <<= 1;
	spec_byte[1] &= ~0x01; //1111 1110
	if (specData->zeroTrack == SV_INHIBIT) {
		spec_byte[1] |= 0x01;
	}

	//byte 2, bit 5-7
	switch (specData->posDecPoint1) {
	case SV_DP_0:
		spec_byte[2] = 0x00;
		break;
	case SV_DP_1:
		spec_byte[2] = 0x01;
		break;
	case SV_DP_2:
		spec_byte[2] = 0x02;
		break;
	case SV_DP_3:
		spec_byte[2] = 0x03;
		break;
	case SV_DP_4:
		spec_byte[2] = 0x04;
		break;
	default:
		spec_byte[2] = 0x00;
		break;
	}

	//byte 2, bit 4, not used
	spec_byte[2] <<= 1;

	//byte 2, bit 2-3
	spec_byte[2] <<= 2;
	spec_byte[2] &= ~0x03; //1111 1100
	switch (specData->reZeroRange) {
	case SV_RZR_2:
		break;
	case SV_RZR_4:
		spec_byte[2] |= 0x01;
		break;
	case SV_RZR_10:
		spec_byte[2] |= 0x02;
		break;
	case SV_RZR_100:
	default:
		spec_byte[2] |= 0x03;
		break;
	}

	//byte 2, bit 1
	spec_byte[2] <<= 1;
	spec_byte[2] &= ~0x01; //1111 1110
	if (specData->reZeroFunc == SV_INHIBIT) {
		spec_byte[2] |= 0x01;
	}

	//byte 2, bit 0
	spec_byte[2] <<= 1;
	spec_byte[2] &= ~0x01; //1111 1110
	if (specData->wtgSinMul1 == SV_WSM_MUL) {
		spec_byte[2] |= 0x01;
	}

	//byte 3, bit 6-7
	switch (specData->negWgtMsk) {
	case SV_NWM_MG9E:
		spec_byte[3] = 0x00;
		break;
	case SV_NWM_MGW:
		spec_byte[3] = 0x01;
		break;
	case SV_NWM_MGF:
		spec_byte[3] = 0x02;
		break;
	case SV_NWM_NU:
	default:
		spec_byte[3] = 0x03;
		break;
	}

	//byte 3, bit 5
	spec_byte[3] <<= 1;
	spec_byte[3] &= ~0x01; //1111 1110
	if (specData->tareRange == SV_TR_100) {
		spec_byte[3] |= 0x01;
	}

	//byte 3, bit 4
	spec_byte[3] <<= 1;
	spec_byte[3] &= ~0x01; //1111 1110
	if (specData->decPointType == SV_TDP_C) {
		spec_byte[3] |= 0x01;
	}

	//byte 3, bit 1-3
	spec_byte[3] <<= 3;
	/*
	spec_byte[3] &= ~0x07; //1111 1000
	switch (specData->posDecPoint2) {
	case SV_DP_0:
		break;
	case SV_DP_1:
		spec_byte[3] |= 0x01;
		break;
	case SV_DP_2:
		spec_byte[3] |= 0x02;
		break;
	case SV_DP_3:
		spec_byte[3] |= 0x03;
		break;
	case SV_DP_4:
		spec_byte[3] |= 0x04;
		break;
	default:
		break;
	}
	*/

	//byte 3, bit 0
	spec_byte[3] <<= 1;
	/*
	spec_byte[3] &= ~0x01; //1111 1110
	if (specData->wtgSinMu2 == SV_WSM_MUL) {
		spec_byte[3] |= 0x01;
	}
	*/

	//byte 4, bit 6-7
	switch (specData->scale1FilterStrn) {
	case SV_FS_LO:
		spec_byte[4] = 0x00;
		break;
	case SV_FS_NM:
		spec_byte[4] = 0x01;
		break;
	case SV_FS_UN:
		spec_byte[4] = 0x02;
		break;
	case SV_FS_HI:
		spec_byte[4] = 0x03;
		break;
	default:
		spec_byte[4] = 0x01;
		break;
	}

	//byte 4, bit 4-5
	spec_byte[4] <<= 2;
	/*
	spec_byte[4] &= ~0x03; //1111 1100
	switch (specData->scale2FilterStrn) {
	case SV_FS_LO:
		spec_byte[4] |= 0;
		break;
	case SV_FS_NM:
		spec_byte[4] |= 1;
		break;
	case SV_FS_UN:
		spec_byte[4] |= 2;
		break;
	case SV_FS_HI:
		spec_byte[4] |= 3;
		break;
	default:
		spec_byte[4] |= 1;
		break;
	}
	*/

	//byte 4, bit 0-3, not used
	spec_byte[4] <<= 4;

	memset(cmdData, '0', LEN_RW_SPEC_DATA);
	ad2k_HexToStr((char *) cmdData, spec_byte, sizeof(spec_byte) / sizeof(spec_byte[0]));
}

static int xonXoffCmd(unsigned char xon_xoff)
{
	if (scale_fd < 0) {
		return -1;
	}

	return write(scale_fd, &xon_xoff, sizeof(xon_xoff));
}

static int sendXCmd(unsigned char xon_xoff)
{
	if (msgq_to_ad_id < 0) {
		return -1;
	}

	if (msgq_send_msg(msgq_to_ad_id, AD2K_MSG_DEFAULT, &xon_xoff, sizeof(xon_xoff) < 0)) {
		return -1;
	}

	return 0;
}

static int sendRCmd(unsigned char cmd, int timeout)
{
	return sendCmd(CMD_R, cmd, NULL, 0, timeout);
}

static int sendWCmd(unsigned char cmd, unsigned char *data, int data_len, int timeout)
{
	return sendCmd(CMD_W, cmd, data, data_len, timeout);
}

static int sendCmd(unsigned char type, unsigned char cmd, unsigned char *data, int data_len, int timeout)
{
	if (msgq_to_ad_id < 0) {
		return -1;
	}

	cmd_saved = cmd;

	unsigned char buf[128];
	memset(buf, 0, sizeof(buf));
	snprintf(buf, sizeof(buf), "%c%c%c", STX, type, cmd);

	int n = strlen(buf);
	if (data != NULL && data_len > 0) {
		int i;
		for (i = 0; i < data_len; i++, n++) {
			buf[n] = data[i];
		}
	}
	buf[n++] = ETX;
	buf[n] = calculateBCC((unsigned char *) buf + 1, n - 2);
	n++;
	if (msgq_send_msg(msgq_to_ad_id, AD2K_MSG_DEFAULT, buf, n) < 0) {
		return -1;
	}

	return 0;

}

static unsigned char calculateBCC(unsigned char *data, int len)
{
	int i;
	unsigned char bcc = 0;

	if (data == NULL || len <= 0) {
		return 0;
	}

	for (i = 0; i < len; i++) {
		bcc ^= data[i];
	}

	if(bcc == 0x00
		|| bcc == 0x11
		|| bcc == 0x13
		|| bcc == 0x02
		|| bcc == 0x03) {

		bcc += 0x20;
	}

	return bcc;
}


static int openPort(struct ad2k_port *portConf)
{
	int fd;
	speed_t baudrate;
	struct termios attr;

	if (portConf == NULL) {
		return -1;
	}
	if (strlen(portConf->portName) <= 0) {
		return -1;
	}

	fd = open(portConf->portName, O_RDWR | O_NOCTTY | O_NDELAY);
	if (fd < 0) {
		printf("error open() %s\n", portConf->portName);
		return -1;
	}

	fcntl(fd, F_SETFL, 0);
	tcgetattr(fd, &attr);

	/* set baudrate */
	switch (portConf->baudRate) {
	case 1200:
		baudrate = B1200;
		break;
	case 2400:
		baudrate = B2400;
		break;
	case 4800:
		baudrate = B4800;
		break;
	case 9600:
		baudrate = B9600;
		break;
	case 19200:
		baudrate = B19200;
		break;
	case 38400:
	default:
		baudrate = B38400;
		break;
	}
	cfsetispeed(&attr, (speed_t) baudrate);
	cfsetospeed(&attr, (speed_t) baudrate);

	/* set databit */
	switch (portConf->dataBit) {
	case 7:
		attr.c_cflag = (attr.c_cflag & ~CSIZE) | CS7;
		break;
	case 8:
	default:
		attr.c_cflag = (attr.c_cflag & ~CSIZE) | CS8;
		break;
	}

	/* set parity */
	attr.c_cflag &= ~(PARENB | PARODD);  /* parity none */
	switch (portConf->parity) {
	case 'O': /* ODD */
		attr.c_cflag |= PARENB | PARODD;
		break;
	case 'E': /* EVEN */
		attr.c_cflag |= PARENB;
		break;
	case 'M':
	case 'S':
	case 'N':
	default:
		break;
	}

	/* set stop bit */
	if (portConf->stopBit == 2) {
		attr.c_cflag |= CSTOPB;
	} else {
		attr.c_cflag &= ~CSTOPB;
	}

	/* Enable the receiver and set local mode...*/
	attr.c_cflag |= CLOCAL | CREAD;

	attr.c_iflag |= INPCK;
	attr.c_iflag &= ~(IXON | IXOFF | IXANY); /* disable software flow control */


	attr.c_lflag &= ~(ICANON | ECHO | ECHOE | ISIG); // raw input
	attr.c_oflag &= ~OPOST; //raw output

	attr.c_cc[VTIME] = 1; /* Timeup 15 seconds*/
	attr.c_cc[VMIN] = 0; /* Update the attr and do it NOW */

	/*
	 * Set the new attr for the port...
	 */
	if (tcsetattr(fd, TCSANOW, &attr) != 0) {
		close(fd);
		return -1;
	}

	return fd;
}

void ad2k_StrToHex(unsigned char *hex, char *str, int n)
{
	int i;
	char buf[4];

	if (hex == NULL || str == NULL) {
		return;
	}

	for (i = 0; i < n; i++) {
		memcpy(buf, &str[i*2], 2);
		buf[2] = '\0';
		hex[i] = ad2k_StrToNum(buf, BASE_HEX) & 0xff;
	}
}

void ad2k_HexToStr(char *str, unsigned char *hex, int n)
{
	int i;
	char buf[4];

	if (hex == NULL || str == NULL) {
		return;
	}

	for (i = 0; i < n; i++) {
		snprintf(buf, sizeof(buf), "%02X", hex[i] & 0xff);
		memcpy(&str[i*2], buf, 2);
	}
}

int ad2k_StrToNum(char *str, int base_fg)
{
	int i, n;
	int val = 0;
	int base;
	int num;
	int sign = 1;

	if (str == NULL) {
		return 0;
	}

	switch (base_fg) {
	case BASE_BIN:
		base = 2;
		break;
	case BASE_DEC:
		base = 10;
		break;
	case BASE_HEX:
		base = 16;
		break;
	default:
		return 0;
	}

	n = strlen(str);
	for (i = 0; i < n; i++) {
		if (base_fg == BASE_BIN
			&& !(str[i] == '0' || str[i] == '1')) {
				return 0;
		}
		if (base_fg == BASE_DEC
			&& !(str[i] >= '0' && str[i] <= '9')) {

				if (str[i] == '-') {
					val = 0;
					sign = -1;
					continue;
				} else if (str[i] == '.' || str[i] == ',') {
					continue;
				} else {
					return 0;
				}
		}
		if (base_fg == BASE_HEX
			&& !(str[i] >= '0' && str[i] <= '9')
			&& !(str[i] >= 'a' && str[i] <= 'f')
			&& !(str[i] >= 'A' && str[i] <= 'F')) {
				return 0;
		}

		if (str[i] >= '0' && str[i] <= '9') {
			num = str[i] - '0';
		} else if (str[i] >= 'a' && str[i] <= 'f') {
			num = str[i] - 'a' + 0xa;
		} else if (str[i] >= 'A' && str[i] <= 'F') {
			num = str[i] - 'A' + 0xa;
		} else {
			num = 0;
		}

		val = (val * base) + num;
	}

	return val * sign;
}

static void cutHeadZero(char *dst, char *src)
{
	if (dst == NULL || src == NULL) {
		return;
	}

	char *p, *q;
	int m;

	if ((p = strchr(src, '-')) != NULL) {
		dst[0] = '-';
		dst[1] = '\0';
		q = p + 1;
		m = 1;
	} else {
		q = src;
		m = 0;
	}

	int n = strlen(src);
	if ((p = strchr(src, '.')) == NULL
		&& (p = strchr(src, ',')) == NULL) {
			p = &src[n];  // '\0'
	}

	for (; q < p; q++) {
		if (*q != '0') {
			break;
		}
	}

	int l = &src[n] - q;
	if (q < p) {
		memcpy(&dst[m], q, l);
		dst[m+l] = '\0';
	} else {
		dst[m] = '0';
		memcpy(&dst[m+1], q, l);
		dst[m+1+l] = '\0';
	}
}

static void cutTailZero(char *dst, char *src)
{
	if (dst == NULL || src == NULL) {
		return;
	}

	char *p, *q;

	if ((p = strchr(src, '.')) == NULL
		&& (p = strchr(src, ',')) == NULL) {
			strcpy(dst, src);
			return;
	}

	int m =  p - src;
	int n = strlen(src);
	memcpy(dst, src, m);
	dst[m] = '\0';
	for (q = &src[n-1]; q > p; q--) {
		if (*q != '0') {
			break;
		}
	}
	if (q > p) {
		int l = q - p + 1;
		memcpy(&dst[m], p, l);
		dst[m+l] = '\0';
	}
}

static void msleep(int msec)
{
	usleep(1000 * msec);
}

static int msgq_create(int key)
{
	return msgget(key, 0777 | IPC_CREAT);
}

static int msgq_remove(int msgq_id)
{
	if (msgctl(msgq_id, IPC_RMID, NULL) < 0) {
		return -1;
	}

	return 0;
}

static int msgq_recv_msg(int msgq_id, void *buf)
{
	struct ad2k_msg_s msg;
	int n;

	if (msgq_id < 0) {
		return -1;
	}

	memset(&msg, 0, sizeof(msg));
	n = msgq_rcv(msgq_id, &msg);
	if (n < 0) {
		return -1;
	}

	if (buf != NULL && n > 0) {
		memcpy(buf, msg.text, n);
	}

	return n;
}

static int msgq_send_msg(int msgq_id, int msg_type, void *buf, int n)
{
	struct ad2k_msg_s msg;

	if (msgq_id < 0 || msg_type <= 0) {
		return -1;
	}

	memset(&msg, 0, sizeof(msg));
	msg.type = msg_type;
	if (buf != NULL && n > 0) {
		memcpy(msg.text, buf, n);
	}

	if (msgq_snd(msgq_id, &msg, n) < 0) {
		return -1;
	}

	return 0;
}

static int msgq_rcv(int msgq_id, struct ad2k_msg_s *msg)
{
	int n;

	if (msgq_id < 0 || msg == NULL) {
		return -1;
	}

	//n = msgrcv(msgq_id, msg, sizeof(msg->text), 0, IPC_NOWAIT);
	n = msgrcv(msgq_id, msg, sizeof(msg->text), 0, 0);
	if (n < 0) {
		return -1;
	}

	return n;
}

static int msgq_snd(int msgq_id, struct ad2k_msg_s *msg, int n)
{
	if (msgq_id < 0 || msg == NULL) {
		return -1;
	}

	//if (msgsnd(msgq_id, msg, n, IPC_NOWAIT) < 0) {
	if (msgsnd(msgq_id, msg, n, 0) < 0) {
		return -1;
	}

	return 0;
}

static void msgq_clear(int msgq_id)
{
	static struct ad2k_msg_s msg;
	//while (msgq_rcv(msgq_id, &msg) >= 0);
	while (msgrcv(msgq_id, &msg, sizeof(msg.text), 0, IPC_NOWAIT) >= 0);
}

static void init_crc32_table()
{
	int i, j;
	unsigned int crc;

	for (i = 0; i < 256; i++) {
		crc = i;
		for (j = 0; j < 8; j++) {
			if (crc & 1) {
				crc = (crc >> 1) ^ 0xEDB88320;
			} else {
				crc = crc >> 1;
			}
		}
		crc32_table[i] = crc;
	}
}

static unsigned int crc32(unsigned char *buf, int len)
{
	static char init = 0;
	unsigned int ret = 0xFFFFFFFF;
	int i;

	if (!init) {
		init_crc32_table();
		init = 1;
	}

	for(i = 0; i < len;i++) {
		ret = crc32_table[((ret & 0xFF) ^ buf[i])] ^ (ret >> 8);
	}

	return ret;
}

static unsigned int calculateChecksum(char *fileName)
{
	unsigned int checksum = 0;
	int fd;

	if (fileName == NULL || strlen(fileName) <= 0) {
		return 0;
	}


	fd = open(fileName, O_RDONLY);
	if (fd < 0) {
		return 0;
	}

	while (1) {
		unsigned char buf;
		int len = read(fd, &buf, 1);
		if (len <= 0) {
			break;
		}

		checksum ^= crc32((unsigned char *) &buf, 1);

	}
	close(fd);

	return checksum;
}

