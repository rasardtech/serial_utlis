
#ifndef _LIBAD2k_H_
#define _LIBAD2k_H_


#define BASE_BIN 0
#define BASE_DEC 1
#define BASE_HEX 2

#define CMD_W  'W'  //Write command
#define CMD_R  'R'  //Read command

#define CMD_RW_SPEC_DATA   'C'  //Configuration data
#define CMD_RW_SPAN_DATA   'D'  //Calibration data
#define CMD_R_AD_VER       'F'  //Firmware version

#define CMD_W_CALB_ZERO    'X'  //Zero calibration
#define CMD_W_CALB_SPAN    'P'  //Span calibration
#define CMD_W_OTCH_TARE    'T'  //One touch tare
#define CMD_W_DIGI_TARE    'U'  //Digital tare
#define CMD_W_PRESET_TARE  'u'  //Prest tare
#define CMD_W_PERCENT_TARE 'V'  //Percent tare
#define CMD_W_ZERO_RST     'Z'  //Zero reset
#define CMD_W_INT_MODE     'I'  //Internal count mode
#define CMD_W_WGT_MODE     'N'  //Weighing mode
#define CMD_W_ZRST_PWR_ON  'z'  //Zero reset when power on
#define CMD_W_VLD_CHKSUM   'd'  //Validate AD Box Driver Checksum
#define CMD_W_SET_CHKSUM   '*'  //Write Driver Checksum
#define CMD_W_CALB_SPAN_G  'Q'  //Span calibration with gravity
#define CMD_W_CRCT_G       'E'  //Correct gravity

#define RSP_RW_OK           '0'  //Affirmative response
#define RSP_RW_BCC_ERR      '1'  //BCC error
#define RSP_RW_CMD_NOT_SPT  '2'  //Command is not supported
#define RSP_W_TR_WRN        '3'  //TARE wrong response(SPEC forbid / out of range)
#define RSP_W_RZ_WRN        '4'  //RE-ZERO wrong response(SPEC forbid / out of range)
#define RSP_RW_SPAN_SW_OFF  '5'  //Span Switch is OFF
#define RSP_W_OTHER         '6'  //Other response (SPAN/ZERO change)
#define RSP_W_CHKSUM_ERR    '7'  //Validate Checksum error

#define LEN_NI_DATA       27 //General weight data, N: Weight mode data, I: Internal mode data
#define	LEN_RW_SPEC_DATA  40
#define	LEN_RW_SPAN_DATA  28
#define	LEN_R_AD_VER       3
#define	LEN_W_DIGI_TARE    8
#define	LEN_W_PRESET_TARE  8
#define	LEN_W_PERCENT_TARE 4
#define	LEN_W_CHKSUM       8
#define	LEN_W_CRCT_G       8

#define NBYTE_NI_STATUS  3
#define NBYTE_SPEC       5

enum ad2k_msg_type {
	AD2K_MSG_DEFAULT = 2000,
	AD2K_MSG_NI,
	AD2K_MSG_SPEC,
	AD2K_MSG_SPAN,
	AD2K_MSG_ADVER,
	AD2K_MSG_RESP,
	AD2K_MSG_DEBUG
};

typedef enum {
	SV_ALLOW,
	SV_INHIBIT,
} SPEC_VAL_AI;

typedef enum {
	SV_WSC_LOOSE,
	SV_WSC_NORMAL,
	SV_WSC_TIGHT,
	SV_WSC_STRINGENT,
} SPEC_VAL_WSC;

typedef enum {
	SV_ZN_GROSS,
	SV_ZN_NET,
} SPEC_VAL_ZN;

typedef enum {
	SV_SR_10,  //±10% F.S.
	SV_SR_20,  //±20% F.S.
	SV_SR_50,  //±50% F.S.
	SV_SR_100, //±100% F.S.
} SPEC_VAL_SR;

typedef enum {
	SV_PTO_OT,  //One Touch Tare Priority
	SV_PTO_DT,  //Digit Tare Priority
} SPEC_VAL_PTO;

typedef enum {
	SV_ACC_0,  //>= Gross 21e & >= Net 5e  
	SV_ACC_1,  //>= Net 1e & Price not 0
} SPEC_VAL_ACC;

typedef enum {
	SV_DP_0,  //00000
	SV_DP_1,  //0000.0
	SV_DP_2,  //000.00
	SV_DP_3,  //00.000
	SV_DP_4,  //0.0000
} SPEC_VAL_DP;

typedef enum {
	SV_RZR_2,   //±2% F.S.
	SV_RZR_4,   //±4% F.S.
	SV_RZR_10,  //±10% F.S.
	SV_RZR_100  //±100% F.S.
} SPEC_VAL_RZR;

typedef enum {
	SV_WSM_SIG, //Single interval
	SV_WSM_MUL  //Multi-interval
} SPEC_VAL_WSM;

typedef enum {
	SV_TDP_P, //'.'
	SV_TDP_C  //','
} SPEC_VAL_TDP;

typedef enum {
	SV_TR_50,  //50%
	SV_TR_100  //100%
} SPEC_VAL_TR;

typedef enum {
	SV_SCC_1,  //Scale 1
	SV_SCC_1_2 //Scale 1 & Scale 2
} SPEC_VAL_SCC;

typedef enum {
	SV_NWM_MG9E, //Minus gross > 9e
	SV_NWM_MGW,  //Minus gross Weight
	SV_NWM_MGF,  //Minus gross > Full scale
	SV_NWM_NU    //not used
} SPEC_VAL_NWM;

typedef enum {
	SV_FS_LO,  //Low
	SV_FS_NM,  //Normal
	SV_FS_UN,  //Upper Normal
	SV_FS_HI   //High
} SPEC_VAL_FS;

struct SpecData {
	SPEC_VAL_WSC    wgtStabCond;      //Weight stability condition
	SPEC_VAL_AI     tareAcc;          //Tare accumulation
	SPEC_VAL_AI     tareSub;          //Tare subtraction
	SPEC_VAL_SR     startRange;       //Start range
	SPEC_VAL_AI     autoZeroReset;    //Auto zero reset
	SPEC_VAL_AI     tareAutoClear;    //Tare auto clear

	SPEC_VAL_PTO    priTareOpe;       //Priority of Tare Operation
	SPEC_VAL_ACC    autoClearCond;    //Auto clear condition
	SPEC_VAL_AI     tareAutoClear2;   //Tare auto clear
	SPEC_VAL_ZN     zeroOn;           //Zero point flag on
	SPEC_VAL_AI     manTareCancel;    //Manual tare cancellation
	SPEC_VAL_AI     digiTare;         //Digital tare
	SPEC_VAL_AI     wgtReset;         //Weight reset when tare
	SPEC_VAL_AI     zeroTrack;        //Zero tracking when tare

	SPEC_VAL_DP     posDecPoint1;     //Position of Decimal Point of scale1
	SPEC_VAL_RZR    reZeroRange;      //Re-Zero range
	SPEC_VAL_AI     reZeroFunc;       //Re-zero function
	SPEC_VAL_WSM    wtgSinMul1;       //Weight single interval or multi-interval of scale1  ※Depends on calibration setting

	SPEC_VAL_NWM    negWgtMsk;        //Negative weight display mask
	SPEC_VAL_SCC    startChnChk;      //Start Channel Check
	SPEC_VAL_TDP    decPointType;     //Type of Decimal point
	SPEC_VAL_TR     tareRange;        //Tare Range
	SPEC_VAL_DP     posDecPoint2;     //Position of Decimal Point of scale2
	SPEC_VAL_WSM    wtgSinMu2;        //Weight single interval or multi-interval of scale2  ※Depends on calibration setting

	SPEC_VAL_FS     scale1FilterStrn;  //Scale1 Filtering strength
	SPEC_VAL_FS     scale2FilterStrn;  //Scale2 Filtering strength
};

struct SpanData {
	int firstWeight; /*First weight range*/
	int spanWeight;  /*weight for span use*/
	int secWeight;   /*second weight range*/
	int e2;
	int e1;
};

struct Response {
	unsigned char type; //'R', 'W'
	unsigned char cmd; //'C', 'D', ...
	unsigned char result; // '0', '1', '2', ...
	unsigned char data[128]; //only for type 'R' when result is '0'
	unsigned int data_len;
};

struct ScaleData {
	char sec1Str[16];
	char sec2Str[16];

	int weight;
	int tare;
	int adCount;
	int irCount;
	int irFg;
	int tareFg;
	int spanSwFg;
	int zeroPointFg;
	int stabilizeFg;
	//int underflowFg;
	//int overflowFg;
	char underOverFlowFg;  //0:normal, 1:under flow, 2:over flow

	int percentTareFg;
	int presetTareFg;
	int digitalTareFg;
	int oneTouchTareFg;
};

struct AD2kData {
	int type;
	union {
		struct ScaleData scaleData;
		struct SpecData scaleSpec;
		struct SpanData scaleSpan;
		struct Response response;
		char adVer[16];
	} data;
};

struct ad2k_port {
	char portName[256];
	int baudRate;
	int dataBit;
	int stopBit;
	char parity;
};

typedef struct ad2k_msg_s {
	enum ad2k_msg_type type;
	char text[256];
} ad2k_msg;


int  ad2k_OpenScale(struct ad2k_port *portConf);
void ad2k_CloseScale(void);
int ad2k_StartScale(void);
void ad2k_StopScale(void);
int ad2k_Xon(void);
int ad2k_Xoff(void);
int ad2k_FetchData(struct AD2kData *ad2kData);


/* 'R' command */
int ad2k_GetSpecData(void);
int ad2k_GetSpanData(void);
int ad2k_GetADVer(void);

/* 'W' command */
int ad2k_SetSpecData(struct SpecData *scale_spec);
int ad2k_SetSpanData(struct SpanData *span_data);

int ad2k_CalbSpan(void);
int ad2k_CalbZero(void);
int ad2k_CalbSpanG(void);
int ad2k_CorrectG(int gravity);

int ad2k_SetOneTouchTare(void);
int ad2k_SetDigitalTare(int tare);
int ad2k_SetPresetTare(int tare);
int ad2k_SetPercentTare(int tare);

int ad2k_ZeroReset(void);
int ad2k_ZeroResetPowerOn(void);

int ad2k_IRCntMode(void);
int ad2k_WgtMode(void);

int ad2k_SetChkSum(unsigned int checkSum);
int ad2k_ValidateChkSum(unsigned int checkSum);
int ad2k_CalculateChkSum(void);
int ad2k_CalculateFileChkSum(char *fileName);
void ad2k_StrToHex(unsigned char *hex, char *str, int n);
void ad2k_HexToStr(char *str, unsigned char *hex, int n);
int  ad2k_StrToNum(char *str, int base_fg);


#endif  /* _LIBAD2k_H_ */

